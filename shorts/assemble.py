"""Shots, a voice and captions into one 1080x1920 video.

The clipping side of this project cuts a source apart; this side builds one up. Same ending
though: a vertical file with burned captions timed to what is actually heard, which is why the
caption and subtitle code is shared rather than written twice.
"""

from __future__ import annotations

from pathlib import Path

from . import clips as clip_lines, higgsfield, media, render, voice

OUT_W, OUT_H = render.OUT_W, render.OUT_H


class AssembleError(Exception):
    pass


def build_command(shots: list[Path], narration: Path | None, ass_path: Path, out_path: Path,
                  seconds: float) -> list[str]:
    """One ffmpeg invocation: every shot to 1080x1920, joined, captioned, with the voice over it."""
    if not shots:
        raise AssembleError("there are no shots to assemble")
    parts, labels = [], []
    for index, _ in enumerate(shots):
        # a generated shot is already vertical, but not always at our size, and never exactly
        parts.append(
            "[%d:v]scale=%d:%d:force_original_aspect_ratio=increase,crop=%d:%d,setsar=1,fps=30[v%d]"
            % (index, OUT_W, OUT_H, OUT_W, OUT_H, index)
        )
        labels.append("[v%d]" % index)
    parts.append("%sconcat=n=%d:v=1:a=0[joined]" % ("".join(labels), len(shots)))
    parts.append(
        "[joined]subtitles='%s'[vout]" % render._escape_for_filter(ass_path)
    )

    inputs: list[str] = []
    for shot in shots:
        inputs += ["-i", str(shot)]
    command = ["ffmpeg", "-y", "-loglevel", "error", *inputs]
    if narration is not None:
        command += ["-i", str(narration)]
        # the voice is the whole soundtrack here: a generated shot has no sound worth keeping.
        # No apad: padding the voice forever and relying on -shortest hangs the encoder, so
        # the picture's own length is set on the output instead and short narration just ends.
        parts.append("[%d:a]aresample=48000[aout]" % len(shots))
    command += [
        "-filter_complex", ";".join(parts),
        "-map", "[vout]",
    ]
    command += ["-map", "[aout]"] if narration is not None else ["-an"]
    command += [
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-color_primaries", "bt709", "-color_trc", "bt709", "-colorspace", "bt709",
    ]
    if narration is not None:
        command += ["-c:a", "aac", "-b:a", "128k", "-ar", "48000"]
    # the video ends when the pictures do, whatever the voice does
    command += ["-t", "%.3f" % seconds]
    command += ["-movflags", "+faststart", str(out_path)]
    return command


def make(script: dict, folder: Path, name: str = "video", tier: int = 1, voice_id: str | None = None,
         subtitle_style: dict | None = None, spent_today: float = 0.0, on_event=None) -> dict:
    """Generate the shots, read the narration, burn the captions, return what it made and cost."""
    media.require_ffmpeg()
    folder = Path(folder)
    shots_dir = folder / "shots"
    said = lambda message: on_event(message) if on_event else None  # noqa: E731

    made, cost, notes = [], 0.0, []
    for index, shot in enumerate(script.get("shots", []), start=1):
        said("shot %d of %d: %s" % (index, len(script["shots"]), shot["prompt"][:60]))
        result = higgsfield.make_shot(
            shot["prompt"], shots_dir / ("shot_%02d.mp4" % index), tier=tier,
            spent_today=spent_today + cost,
        )
        made.append(result.path)
        cost += result.cost
        notes.extend(result.notes)

    narration_path, words = None, []
    text = (script.get("narration") or "").strip()
    if text:
        said("recording the narration")
        narration_path = folder / ("%s_voice.mp3" % name)
        words = voice.synthesize(text, voice_id or voice.DEFAULT_VOICES["tiktok"], narration_path)

    picture_seconds = sum(shot["seconds"] for shot in script.get("shots", []))
    lines = clip_lines.subtitle_lines(words, [], clip_end=picture_seconds) if words else []
    ass_path = folder / ("%s.ass" % name)
    ass_path.parent.mkdir(parents=True, exist_ok=True)
    ass_path.write_text(
        render.build_ass(lines, subtitle_style or render_style()), encoding="utf-8"
    )

    out_path = folder / ("%s.mp4" % name)
    said("assembling %d shots into %dx%d" % (len(made), OUT_W, OUT_H))
    media.run(build_command(made, narration_path, ass_path, out_path, picture_seconds))

    spoken = words[-1].end if words else 0.0
    if spoken > picture_seconds + 0.3:
        notes.append(
            "the narration runs %.1fs against %.1fs of picture - the end is cut off"
            % (spoken, picture_seconds)
        )
    return {
        "path": str(out_path),
        "seconds": picture_seconds,
        "cost": round(cost, 4),
        "shots": [str(path) for path in made],
        "mocked": any("mock" in note for note in notes),
        "notes": notes,
    }


def render_style() -> dict:
    from .config import DEFAULT_SUBTITLE_STYLE

    return dict(DEFAULT_SUBTITLE_STYLE)
