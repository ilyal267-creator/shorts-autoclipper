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
        return RedirectResponse("/runs", status_code=303)  # the progress page arrives with the worker
