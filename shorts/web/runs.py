"""New run: upload a source, check what it is, choose settings, queue it for the worker."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import urllib.parse
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from .. import config as config_mod, media
from ..config import PLATFORMS
from ..voice import DEFAULT_VOICES
from . import db

PLATFORM_NAMES = {"tiktok": "TikTok", "instagram_reels": "Instagram Reels", "youtube_shorts": "YouTube Shorts"}
VOICE_NAMES = {"TX3LPaxmHKxFdv7VOQHJ": "Liam", "Xb7hH8MSUJpSbSDYk0k2": "Alice", "nPczCjzI2devNBz1zQrb": "Brian"}
FRAMING = {"auto": None, "center_crop": "center_crop", "fit": "fit"}
# Errors travel back to the form as a code, never as text from the URL.
START_ERRORS = {
    "rights": "Confirm you have the rights to this video before starting.",
    "platforms": "Pick at least one platform.",
    "form": "Something in the form didn't make sense. Try again.",
    "track": "That audio track can't be read.",
    "clips": "Choose between 1 and 5 clips.",
    "cap": "You've used today's runs. More tomorrow.",
}


def limits() -> dict:
    return {
        "max_bytes": int(os.getenv("SHORTS_MAX_UPLOAD_BYTES", 4 * 1024**3)),
        "max_minutes": float(os.getenv("SHORTS_MAX_SOURCE_MINUTES", 60)),
        "runs_per_day": int(os.getenv("SHORTS_RUNS_PER_DAY", 3)),
    }


def owned_run(db_path, run_id: str, user_id: int):
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT * FROM runs WHERE id = ? AND user_id = ?", (run_id, user_id)).fetchone()
    finally:
        conn.close()
    if row is None:  # someone else's run and no run at all look the same
        raise HTTPException(404)
    return row


def runs_started_today(db_path, user_id: int) -> int:
    conn = db.connect(db_path)
    try:
        return conn.execute(
            "SELECT count(*) FROM runs WHERE user_id = ? AND queued_at >= date('now')", (user_id,)
        ).fetchone()[0]
    finally:
        conn.close()


def safe_name(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    return re.sub(r"[^\w.\- ()]+", "_", base).strip(" .")[:120] or "video"


def duration_text(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return "%d:%02d:%02d" % (hours, minutes, secs) if hours else "%d:%02d" % (minutes, secs)


def size_text(size: int) -> str:
    for unit, scale in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= scale:
            return "%.1f %s" % (size / scale, unit)
    return "%d B" % size


STATUS_PILLS = {
    "uploaded": ("Not started", ""),
    "queued": ("Waiting to start", ""),
    "needs_review": ("Needs review", "accent"),
    "no_clips": ("No clips", "bad"),
    "failed": ("Failed", "bad"),
    "cancelled": ("Cancelled", ""),
}


def list_runs(db_path, user_id: int) -> list[dict]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM runs WHERE user_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 100", (user_id,)
        ).fetchall()
    finally:
        conn.close()

    out = []
    for run in rows:
        probe = json.loads(run["probe_json"])
        raw = json.loads(run["config_json"] or "{}")
        summary = json.loads(run["summary_json"] or "{}")
        tracks = sum(1 for c in probe.get("audio_codecs", []) if c != "none")
        facts = [duration_text(probe.get("duration", 0)), "%s×%s" % (probe.get("width"), probe.get("height"))]
        if probe.get("hdr"):
            facts.append("HDR")
        if tracks > 1:
            facts.append("%d audio tracks" % tracks)
        platforms = [a["platform"] for a in raw.get("connected_accounts", [])]
        label, tone = STATUS_PILLS.get(run["status"], (run["status"], ""))
        stage_share = None
        if run["status"] == "running":
            keys = [k for k, _ in STAGE_LABELS if raw.get("voiceover", {}).get("enabled") or k != "voiceover"]
            label = dict(STAGE_LABELS).get(run["stage"], "Starting")
            stage_share = int(100 * (keys.index(run["stage"]) + 0.5) / len(keys)) if run["stage"] in keys else 3
        out.append({
            "id": run["id"],
            "href": "/runs/%s/setup" % run["id"] if run["status"] == "uploaded" else "/runs/%s" % run["id"],
            "name": run["source_name"],
            "facts": " · ".join(facts),
            "started": (run["queued_at"] or run["created_at"]).replace(" ", "T") + "Z",
            "clips": len(summary.get("clips", [])) if run["status"] in ("needs_review", "no_clips") else None,
            "platforms": [(p, PLATFORM_NAMES[p]) for p in platforms if p in PLATFORM_NAMES],
            "mode": "Draft for approval" if raw else "—",
            "status": run["status"],
            "label": label,
            "tone": tone,
            "share": stage_share,
        })
    return out


def register(app, page) -> None:
    @app.get("/runs/new")
    def new_run(request: Request):
        cap = limits()
        used = runs_started_today(app.state.db_path, request.state.user["id"])
        return page(
            request, "new_run.html", nav="new", runs_left=max(0, cap["runs_per_day"] - used),
            cap=cap, max_size=size_text(cap["max_bytes"]),
        )

    @app.post("/api/uploads")
    async def upload(request: Request):
        """The raw file as the request body, streamed straight to disk under the cap."""
        user = request.state.user
        cap = limits()
        declared = int(request.headers.get("content-length") or 0)
        if declared > cap["max_bytes"]:
            return JSONResponse({"error": "That file is over the %s limit." % size_text(cap["max_bytes"])}, status_code=413)

        try:
            media.require_ffmpeg()
        except media.MediaError:
            # the server's fault, not the file's: say so instead of blaming the upload
            return JSONResponse({"error": "The server can't process videos right now. Try again later."}, status_code=503)

        name = safe_name(urllib.parse.unquote(request.headers.get("x-filename") or "video"))
        run_id = secrets.token_urlsafe(9)
        folder = app.state.data_dir / "runs" / run_id
        folder.mkdir(parents=True)
        source = folder / ("source" + (Path(name).suffix.lower() or ".mp4"))
        written = 0
        try:
            with open(source, "wb") as out:
                async for chunk in request.stream():
                    written += len(chunk)
                    if written > cap["max_bytes"]:
                        raise ValueError("over the %s limit" % size_text(cap["max_bytes"]))
                    out.write(chunk)
            if written == 0:
                raise ValueError("the upload was empty")
            try:
                probe = await run_in_threadpool(media.probe, str(source))
            except media.MediaError:
                raise ValueError("that file couldn't be read as a video")
            if probe.duration > cap["max_minutes"] * 60:
                raise ValueError("videos can be up to %d minutes during the test" % cap["max_minutes"])
            if probe.audio_tracks == 0:
                raise ValueError("that video has no sound to cut from")
        except ValueError as exc:
            shutil.rmtree(folder, ignore_errors=True)
            status = 413 if "limit" in str(exc) else 422
            return JSONResponse({"error": str(exc)[0].upper() + str(exc)[1:] + "."}, status_code=status)

        conn = db.connect(app.state.db_path)
        try:
            with conn:
                conn.execute(
                    """INSERT INTO runs (id, user_id, status, source_name, source_path, source_bytes, probe_json)
                       VALUES (?, ?, 'uploaded', ?, ?, ?, ?)""",
                    (run_id, user["id"], name, str(source), written, json.dumps(probe.__dict__)),
                )
        finally:
            conn.close()
        return {"run_id": run_id, "next": "/runs/%s/setup" % run_id}

    @app.get("/runs/{run_id}/setup")
    def setup(request: Request, run_id: str, error: str = ""):
        run = owned_run(app.state.db_path, run_id, request.state.user["id"])
        if run["status"] != "uploaded":
            return RedirectResponse("/runs", status_code=303)
        probe = json.loads(run["probe_json"])
        tracks = [
            {"index": i, "codec": codec, "readable": codec != "none"}
            for i, codec in enumerate(probe["audio_codecs"])
        ]
        cap = limits()
        used = runs_started_today(app.state.db_path, request.state.user["id"])
        return page(
            request, "setup_run.html", nav="new", run=run, probe=probe, tracks=tracks,
            readable_tracks=[t for t in tracks if t["readable"]],
            duration=duration_text(probe["duration"]), size=size_text(run["source_bytes"]),
            platforms=[(p, PLATFORM_NAMES[p], VOICE_NAMES[DEFAULT_VOICES[p]]) for p in PLATFORMS],
            runs_left=max(0, cap["runs_per_day"] - used), error=START_ERRORS.get(error),
        )

    @app.post("/runs/{run_id}/start")
    async def start(request: Request, run_id: str):
        user = request.state.user
        run = owned_run(app.state.db_path, run_id, user["id"])
        if run["status"] != "uploaded":
            raise HTTPException(409, "this run has already started")
        form = await request.form()

        def refuse(code: str):
            return RedirectResponse("/runs/%s/setup?error=%s" % (run_id, code), status_code=303)

        if form.get("rights") != "yes":
            return refuse("rights")
        platforms = [p for p in form.getlist("platforms") if p in PLATFORMS]
        if not platforms:
            return refuse("platforms")
        codecs = json.loads(run["probe_json"])["audio_codecs"]
        try:
            track = int(form.get("audio_track", 0))
            clip_count = int(form.get("clip_count", 3))
        except ValueError:
            return refuse("form")
        if not (0 <= track < len(codecs)) or codecs[track] == "none":
            return refuse("track")
        if not 1 <= clip_count <= 5:
            return refuse("clips")
        framing = form.get("framing", "auto")
        if framing not in FRAMING:
            return refuse("form")
        if runs_started_today(app.state.db_path, user["id"]) >= limits()["runs_per_day"]:
            return refuse("cap")

        raw = {
            "source_video": run["source_path"],
            "connected_accounts": [{"platform": p, "account_id": "pending", "handle": ""} for p in platforms],
            "clip_count": clip_count,
            "clip_length_range": [15, 45],
            # Publishing needs a connected account and waits for the review screen, so every
            # run starts as drafts; nothing posts from the run itself.
            "posting_mode": "draft_for_approval",
            "rights_confirmed": True,
            "audio_track": track,
            "reframe_mode": FRAMING[framing],
            "voiceover": {"enabled": form.get("voiceover") == "on", "disclose": True},
            "output_dir": str(Path(run["source_path"]).parent / "out"),
        }
        config_mod.from_dict(raw)  # refuses anything the pipeline itself would refuse

        conn = db.connect(app.state.db_path)
        try:
            with conn:
                conn.execute(
                    """UPDATE runs SET status = 'queued', config_json = ?, queued_at = datetime('now')
                       WHERE id = ? AND status = 'uploaded'""",
                    (json.dumps(raw), run_id),
                )
        finally:
            conn.close()
        return RedirectResponse("/runs/%s" % run_id, status_code=303)

    @app.get("/runs/{run_id}")
    def run_page(request: Request, run_id: str):
        run = owned_run(app.state.db_path, run_id, request.state.user["id"])
        if run["status"] == "uploaded":
            return RedirectResponse("/runs/%s/setup" % run_id, status_code=303)
        return page(request, "progress.html", nav="runs", run=run, progress=progress(app.state.db_path, run))

    @app.get("/api/runs/{run_id}/progress")
    def run_progress(request: Request, run_id: str):
        return progress(app.state.db_path, owned_run(app.state.db_path, run_id, request.state.user["id"]))

    @app.post("/runs/{run_id}/cancel")
    def cancel(request: Request, run_id: str):
        owned_run(app.state.db_path, run_id, request.state.user["id"])
        conn = db.connect(app.state.db_path)
        try:
            with conn:
                # queued: nothing has started, so it simply never will; running: the worker
                # stops at the next stage it enters
                conn.execute(
                    "UPDATE runs SET status = 'cancelled', finished_at = datetime('now') WHERE id = ? AND status = 'queued'",
                    (run_id,),
                )
                conn.execute(
                    "UPDATE runs SET cancel_requested_at = datetime('now') WHERE id = ? AND status = 'running'",
                    (run_id,),
                )
        finally:
            conn.close()
        return RedirectResponse("/runs/%s" % run_id, status_code=303)


STAGE_LABELS = [
    ("probe", "Read the video"),
    ("transcribe", "Transcribe"),
    ("plan", "Pick the clips"),
    ("copy", "Write hooks, captions and narration"),
    ("compliance", "Check compliance"),
    ("voiceover", "Record voiceovers"),
    ("render", "Render"),
    ("deliver", "Save drafts for review"),
]
# a whole token that starts at a drive letter or a root slash, down to its last separator
ABSOLUTE_PATH = re.compile(r"(?<!\S)(?:[A-Za-z]:)?[\\/](?:[^\s\\/]+[\\/])*([^\s\\/]+)")
PER_CLIP = {"copy", "compliance", "voiceover", "render", "deliver"}
HEADLINES = {
    "queued": "Waiting to start",
    "running": "Working",
    "needs_review": "Drafts ready",
    "no_clips": "No clips cleared the bar",
    "failed": "This run failed",
    "cancelled": "Cancelled",
}


def progress(db_path, run) -> dict:
    """Everything the progress screen shows, from the run row and its event log."""
    conn = db.connect(db_path)
    try:
        events = conn.execute(
            "SELECT at, stage, message, data_json FROM run_events WHERE run_id = ? ORDER BY id", (run["id"],)
        ).fetchall()
    finally:
        conn.close()

    narrated = json.loads(run["config_json"] or "{}").get("voiceover", {}).get("enabled")
    keys = [key for key, _ in STAGE_LABELS if narrated or key != "voiceover"]
    clips = next((json.loads(e["data_json"]) for e in reversed(events) if e["stage"] == "plan" and e["data_json"]), [])
    current = run["stage"] if run["status"] == "running" else None
    reached = keys.index(run["stage"]) if run["stage"] in keys else -1
    clip_now = next((e["message"].split(":")[0] for e in reversed(events) if e["stage"] in PER_CLIP), None)
    finished_ok = run["status"] in ("needs_review", "no_clips")

    stages = []
    for key, label in STAGE_LABELS:
        if key not in keys:
            continue
        position = keys.index(key)
        if finished_ok or position < reached:
            state = "done"
        elif key == current:
            state = "active"
        elif position == reached:  # where a failed or cancelled run stopped
            state = "stopped"
        else:
            state = "pending"
        detail = ""
        if key == "plan" and state == "done" and clips:
            detail = "%d found" % len(clips)
        elif state == "active" and key in PER_CLIP and clip_now and clips:
            ids = [c["clip_id"] for c in clips]
            if clip_now in ids:
                detail = "Clip %d of %d" % (ids.index(clip_now) + 1, len(ids))
        stages.append({"key": key, "label": label, "state": state, "detail": detail})

    return {
        "status": run["status"],
        "headline": HEADLINES.get(run["status"], run["status"]),
        "terminal": run["status"] not in ("queued", "running"),
        "cancelling": bool(run["cancel_requested_at"]) and run["status"] == "running",
        "error": hide_paths(run["error"]) if run["error"] else None,
        "stages": stages,
        "clips": [
            {**c, "span": "%s–%s" % (duration_text(c["start"]), duration_text(c["end"])), "length": "%.1fs" % c["duration"]}
            for c in clips
        ],
        "log": ["%s  %s" % (e["at"][11:19], hide_paths(e["message"])) for e in events[-60:]],
    }


def hide_paths(message: str) -> str:
    """Testers see file names, never where the server keeps them."""
    return ABSOLUTE_PATH.sub(r"\1", message)
