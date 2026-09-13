"""The pipeline itself (§3): ingest -> select -> write -> screen -> render -> publish -> report.

One clip failing never takes the run down; only a §4.5 halt does.
"""

from __future__ import annotations

import json
import os
import traceback
from datetime import datetime, timezone
from pathlib import Path

from . import agent, compliance, media, publish as publishing, render as rendering
from .clips import (
    Clip,
    duplicate_copy,
    platform_notes,
    retime,
    seams,
    subtitle_lines,
    validate,
)
from .config import Config
from .schedule import Queue, slots
from .transcribe import transcribe


class Run:
    """`on_event(stage, message, data)` hears every log line as it happens; `stage` is set when
    the run enters a stage (probe, transcribe, plan, copy, compliance, voiceover, render,
    deliver) and None otherwise. `creds` maps platform -> that account's credentials; None
    means the CLI's own, read from the environment."""

    def __init__(self, cfg: Config, on_event=None, creds: dict | None = None):
        self.cfg = cfg
        self.on_event = on_event
        self.creds = creds
        self.log: list[str] = []
        self.flags: list[str] = list(cfg.flags)
        self.excluded: list[dict] = []
        self.out_dir = Path(cfg.output_dir)
        self.queue = Queue(self.out_dir / "queue.json")
        self.posted: set[tuple[str, str, str]] = set()
        self.voices: dict[str, str] = {}  # platform -> ElevenLabs voice id, chosen once per run
        self.transcript = None

    def note(self, message: str, stage: str | None = None, data=None) -> None:
        stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.log.append("[%s] %s" % (stamp, message))
        if self.on_event:
            self.on_event(stage, message, data)

    # ----------------------------------------------------------------- stages

    def execute(self) -> dict:
        cfg = self.cfg
        provider = agent.provider()
        self.note("provider=%s posting_mode=%s" % (provider, cfg.posting_mode))
        if provider == "mock":
            self.flags.append(
                "MOCK provider: no model ran — every clip choice and every line of copy is a "
                "placeholder and must not be published"
            )

        self.note("reading the source", stage="probe")
        probe = media.probe(cfg.source_video)
        self.note(
            "source %dx%d %.1fs, %d audio track(s)%s"
            % (
                probe.width,
                probe.height,
                probe.duration,
                probe.audio_tracks,
                ", HDR (tone-mapped to SDR)" if probe.hdr else "",
            )
        )
        if probe.audio_tracks == 0:
            raise media.MediaError("%s has no audio track to cut from" % cfg.source_video)
        if not probe.can_decode(cfg.audio_track):
            raise media.MediaError(
                "audio_track %d cannot be cut from — the source's audio streams are %s, "
                "numbered from 0, and \"none\" means ffmpeg has no decoder for it"
                % (cfg.audio_track, probe.audio_codecs)
            )
        if probe.audio_tracks > 1:
            self.note("using audio track %d of %d" % (cfg.audio_track, probe.audio_tracks))
            self.flags.append(
                "source has %d audio tracks (game, mic and desktop are usually separate); track %d "
                "was used — set audio_track in the config if that is the wrong one"
                % (probe.audio_tracks, cfg.audio_track)
            )

        self.note("transcribing", stage="transcribe")
        transcript = transcribe(cfg.source_video, cfg.source_transcript, cfg.language, cfg.audio_track)
        self.transcript = transcript
        self.note("transcript: %d words via %s (%s)" % (len(transcript.words), transcript.source, transcript.language))

        self.note("choosing clips", stage="plan")
        plan = agent.plan_clips(cfg, transcript, probe.width, probe.height)
        if plan.get("notes"):
            self.note("selection notes: %s" % plan["notes"])
        self.excluded.extend(plan.get("excluded_segments", []))

        clips = self._build_clips(plan, transcript)
        self.note(
            "%d clip(s) chosen" % len(clips),
            stage="plan",
            data=[
                {"clip_id": c.clip_id, "start": c.start, "end": c.end, "duration": c.duration, "reason": c.selection_reason}
                for c in clips
            ],
        )
        if cfg.reframe_mode:
            self.note("reframe_mode=%s from the config overrides the plan" % cfg.reframe_mode)
        if len(clips) < cfg.clip_count:
            self.flags.append(
                "returned %d of %d requested clips — the rest did not clear the quality bar"
                % (len(clips), cfg.clip_count)
            )

        results = []
        for index, clip in enumerate(clips):
            try:
                results.append(self._process(clip, index, transcript, probe, clips))
            except compliance.HaltRun:
                raise
            except Exception as exc:  # §8: one clip's failure is not the run's failure
                self.note("%s failed: %s" % (clip.clip_id, exc))
                self.flags.append("%s failed during processing: %s" % (clip.clip_id, exc))
                results.append(
                    {
                        "clip_id": clip.clip_id,
                        "start": clip.start,
                        "end": clip.end,
                        "selection_reason": clip.selection_reason,
                        "confidence": clip.confidence,
                        "asset_ref": clip.asset_ref,
                        "platforms": {},
                        "error": str(exc),
                    }
                )
        return self._summary(results, provider)

    def _build_clips(self, plan: dict, transcript) -> list[Clip]:
        cfg = self.cfg
        built: list[Clip] = []
        for i, raw in enumerate(plan.get("clips", [])):
            clip = Clip(
                clip_id="clip_%d" % (i + 1),
                start=agent.snap(float(raw["start"]), transcript, "start"),
                end=agent.snap(float(raw["end"]), transcript, "end"),
                cuts=[(float(c["start"]), float(c["end"])) for c in raw.get("cuts", [])],
                reframe=(
                    {"mode": cfg.reframe_mode}
                    if cfg.reframe_mode
                    else raw.get("reframe") or {"mode": "center_crop"}
                ),
                audio=raw.get("audio", "preserve"),
                selection_reason=raw.get("selection_reason", ""),
                confidence=raw.get("confidence", "low"),
            )
            built.append(clip)

        kept = []
        for clip in built:
            problems = validate(clip, cfg.clip_length_range, built)
            if problems:
                self.note("%s dropped: %s" % (clip.clip_id, "; ".join(problems)))
                self.excluded.append(
                    {"start": clip.start, "end": clip.end, "reason": "; ".join(problems)}
                )
                continue
            kept.append(clip)
        return kept

    def _process(self, clip: Clip, index: int, transcript, probe, siblings: list[Clip]) -> dict:
        cfg = self.cfg
        words = retime(transcript.words_between(clip.start, clip.end), clip.keeps)
        clip_text = " ".join(w.text for w in words)
        self.note("%s: writing copy" % clip.clip_id, stage="copy")

        sibling_hooks = [
            s.copy.get("tiktok", {}).get("hook", "") for s in siblings if s.copy and s is not clip
        ]
        clip.copy = agent.write_copy(
            cfg, clip_text, index, [h for h in sibling_hooks if h], clip.duration
        )
        for problem in duplicate_copy(clip.copy):
            self.flags.append("%s: %s — rewrite before publishing" % (clip.clip_id, problem))
        for platform in {a["platform"] for a in cfg.connected_accounts}:
            for note in platform_notes(platform, clip.copy.get(platform, {})):
                self.flags.append("%s: %s" % (clip.clip_id, note))

        self.note("%s: compliance check" % clip.clip_id, stage="compliance")
        verdict = compliance.check(cfg, clip_text, clip.copy, agent.safety_review(cfg, clip_text, clip.copy))
        self.flags.extend("%s: %s" % (clip.clip_id, flag) for flag in verdict.flags)
        if not verdict.ok:
            self.note("%s excluded: %s" % (clip.clip_id, "; ".join(verdict.reasons)))
            clip.excluded_reason = "; ".join(verdict.reasons)
            self.excluded.append(
                {"start": clip.start, "end": clip.end, "reason": clip.excluded_reason}
            )
            return self._clip_row(clip, {p["platform"]: {"status": "excluded", "reason": clip.excluded_reason} for p in cfg.connected_accounts})

        emphasis = clip.copy.get("tiktok", {}).get("emphasis_words", [])
        clip.subtitles = subtitle_lines(
            words, emphasis, clip_end=clip.duration, seam_points=seams(clip.keeps)
        )
        for note in rendering.audio_notes(clip):
            self.flags.append(note)

        if cfg.narrated:
            self._narrate(clip, probe)
        else:
            self.note("%s: rendering" % clip.clip_id, stage="render")
            clip.asset_ref = rendering.render(clip, cfg, probe.width, probe.height, probe.hdr)
            self.note("%s rendered %.1fs -> %s" % (clip.clip_id, clip.duration, clip.asset_ref))

        return self._clip_row(clip, self._deliver(clip))

    def _narrate(self, clip: Clip, probe) -> None:
        """One render per platform: its own voice reading its own script, captioned to match."""
        from . import voice

        cfg = self.cfg
        platforms = sorted({a["platform"] for a in cfg.connected_accounts})
        if not self.voices:
            self.voices = voice.pick_voices(platforms, cfg.voiceover.get("voices") or {})
            self.note("voices: %s" % ", ".join("%s=%s" % kv for kv in sorted(self.voices.items())))
        model = cfg.voiceover.get("model") or voice.DEFAULT_MODEL
        assets_dir = self.out_dir / "assets"
        # How loud the clip's own sound sits under the narration. Music and ambience belong in
        # the mix; another person talking does not — at -18dB a speaker is still intelligible
        # in every pause, and two voices at once reads as a mistake. Set it to override.
        spoke = bool(transcript_words(clip, self.transcript))
        bed_db = cfg.voiceover.get("original_audio_db")
        if bed_db is None:
            bed_db = -32.0 if spoke else -14.0
        self.note(
            "%s: original audio %.0fdB under the narration (%s)"
            % (clip.clip_id, bed_db, "speech in the clip" if spoke else "no speech — music/ambience kept")
        )

        for platform in platforms:
            script = (clip.copy.get(platform) or {}).get("voiceover", "").strip()
            if not script:
                self.flags.append("%s: no narration written for %s" % (clip.clip_id, platform))
                continue
            narration = assets_dir / ("%s_%s_voice.mp3" % (clip.clip_id, platform))
            self.note("%s: recording the %s voiceover" % (clip.clip_id, platform), stage="voiceover")
            words = voice.synthesize(script, self.voices[platform], narration, model)
            spoken = words[-1].end if words else 0.0
            if spoken > clip.duration + 0.3:
                self.flags.append(
                    "%s: %s narration runs %.1fs against a %.1fs clip — the end is cut off"
                    % (clip.clip_id, platform, spoken, clip.duration)
                )
            emphasis = (clip.copy.get(platform) or {}).get("emphasis_words", [])
            captions = subtitle_lines(words, emphasis, clip_end=clip.duration)
            self.note("%s: rendering for %s" % (clip.clip_id, platform), stage="render")
            clip.assets[platform] = rendering.render(
                clip,
                cfg,
                probe.width,
                probe.height,
                probe.hdr,
                variant=platform,
                subtitles=captions,
                voiceover=narration,
                bed_db=float(bed_db),
            )
            self.note(
                "%s rendered for %s: %.1fs of narration over %.1fs"
                % (clip.clip_id, platform, spoken, clip.duration)
            )

    def _deliver(self, clip: Clip) -> dict:
        cfg = self.cfg
        out: dict = {}
        dry_run = os.getenv("SHORTS_DRY_RUN") == "1"
        self.note("%s: delivering (%s)" % (clip.clip_id, cfg.posting_mode), stage="deliver")

        for account in cfg.connected_accounts:
            platform, account_id = account["platform"], account["account_id"]
            key = (clip.clip_id, platform, account_id)
            if key in self.posted:
                continue  # §4.7: never the same clip to the same account twice in one run
            self.posted.add(key)

            meta = dict(clip.copy.get(platform, {}))
            meta["public_asset_base_url"] = cfg.public_asset_base_url
            # A realistic synthetic voice is declared, not left for the platform to discover.
            meta["synthetic_media"] = cfg.narrated and bool(cfg.voiceover.get("disclose", True))
            asset = clip.assets.get(platform) or clip.asset_ref
            if not asset:
                out[platform] = {"status": "failed", "error": "no render for this platform"}
                continue

            if cfg.posting_mode == "draft_for_approval":
                out[platform] = {"status": "draft", "asset_ref": asset, **_text_fields(meta)}
                continue

            if cfg.posting_mode == "schedule":
                when = slots(
                    cfg.posting_schedule,
                    1,
                    datetime.now(timezone.utc),
                    taken=self.queue.booked(platform, account_id),
                )
                if not when:
                    out[platform] = {"status": "failed", "error": "no free slot in the next 30 days"}
                    continue
                queued_id = self.queue.add(platform, account_id, asset, meta, when[0])
                out[platform] = {
                    "status": "scheduled",
                    "scheduled_for": when[0].isoformat(),
                    "queued_id": queued_id,
                    "asset_ref": asset,
                    **_text_fields(meta),
                }
                continue

            result = publishing.publish(
                platform, account_id, asset, meta, dry_run=dry_run, creds=self._creds(platform)
            )
            out[platform] = {
                "status": result.status,
                "post_id": result.post_id,
                "error": result.error,
                "asset_ref": asset,
                **_text_fields(meta),
            }
            if result.status == "failed":
                self.note("%s -> %s failed: %s" % (clip.clip_id, platform, result.error))
        return out

    def _creds(self, platform: str) -> dict | None:
        return None if self.creds is None else self.creds.get(platform, {})

    def _clip_row(self, clip: Clip, platforms: dict) -> dict:
        return {
            "clip_id": clip.clip_id,
            "start": clip.start,
            "end": clip.end,
            "duration": clip.duration,
            # Without these the summary cannot explain why duration < end - start, and the
            # clip cannot be rebuilt from its own record.
            "cuts": [{"start": s, "end": e} for s, e in clip.cuts],
            "selection_reason": clip.selection_reason,
            "confidence": clip.confidence,
            "asset_ref": clip.asset_ref,
            "platforms": platforms,
        }

    def _summary(self, clips: list[dict], provider: str) -> dict:
        return {
            "source_video": self.cfg.source_video,
            "generated_by": provider,
            "posting_mode": self.cfg.posting_mode,
            "voices": self.voices,  # platform -> ElevenLabs voice id; empty when not narrated
            "clips": clips,
            "excluded_segments": self.excluded,
            "flags_for_human_review": self.flags,
            "log": self.log,
        }


def transcript_words(clip: Clip, transcript) -> list:
    """What was actually said inside this clip, after its cuts."""
    if transcript is None:
        return []
    return retime(transcript.words_between(clip.start, clip.end), clip.keeps)


def _text_fields(meta: dict) -> dict:
    return {
        "hook": meta.get("hook") or None,
        "title": meta.get("title") or None,
        "caption": meta.get("caption"),
        "hashtags": meta.get("hashtags", []),
        # kept on the draft so a later publish still declares the AI voice
        "synthetic_media": bool(meta.get("synthetic_media")),
        **({"voiceover": meta["voiceover"]} if meta.get("voiceover") else {}),
    }


def execute(cfg: Config, on_event=None, creds: dict | None = None) -> dict:
    run = Run(cfg, on_event, creds)
    try:
        summary = run.execute()
    except compliance.HaltRun as exc:
        run.note("RUN HALTED: %s" % exc)
        summary = run._summary([], agent.provider())
        summary["halted"] = str(exc)
    except Exception as exc:
        run.note("RUN FAILED: %s" % exc)
        summary = run._summary([], agent.provider())
        summary["error"] = str(exc)
        summary["traceback"] = traceback.format_exc()

    out_dir = Path(cfg.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (out_dir / "run.log").write_text("\n".join(summary["log"]) + "\n", encoding="utf-8")
    return summary


def publish_draft(
    entry: dict,
    platform: str,
    account_id: str,
    creds: dict | None = None,
    dry_run: bool = False,
    public_asset_base_url: str | None = None,
) -> publishing.Result:
    """Publish one draft from a summary (`clip["platforms"][platform]`), as it reads now.

    The review screen edits title, caption and hashtags in place; whatever the entry holds is
    what posts. The narration and burned captions are already in the render.
    """
    meta = {
        "hook": entry.get("hook"),
        "title": entry.get("title"),
        "caption": entry.get("caption"),
        "hashtags": entry.get("hashtags") or [],
        "synthetic_media": bool(entry.get("synthetic_media")),
        "public_asset_base_url": public_asset_base_url,
    }
    return publishing.publish(platform, account_id, entry.get("asset_ref"), meta, dry_run, creds)


def drain_queue(cfg: Config, creds: dict | None = None) -> list[dict]:
    """Post everything whose scheduled time has arrived."""
    queue = Queue(Path(cfg.output_dir) / "queue.json")
    dry_run = os.getenv("SHORTS_DRY_RUN") == "1"
    done = []
    for row in queue.due(datetime.now(timezone.utc)):
        result = publishing.publish(
            row["platform"],
            row["account_id"],
            row["asset_ref"],
            row["metadata"],
            dry_run=dry_run,
            creds=None if creds is None else creds.get(row["platform"], {}),
        )
        queue.mark(row["queued_id"], result.status, result.error or result.post_id)
        done.append({**row, "status": result.status, "post_id": result.post_id, "error": result.error})
    return done
