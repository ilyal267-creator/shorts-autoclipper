"""One command, start to finish.

A URL in, drafts out. No settings screen: whatever the tester has saved is what it uses, and
when nothing is saved it uses the standard defaults and says so. A video address is fetched as
it stands; a channel address takes that channel's newest upload, and keeps whatever rights
record that channel already carries.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from fastapi import Request
from fastapi.responses import RedirectResponse

from .. import media, sources
from . import channels, db, settings
from .runs import limits, runs_started_today

VIDEO_MARKS = ("watch?v=", "youtu.be/", "/shorts/", "/live/")


def looks_like_a_video(url: str) -> bool:
    return any(mark in url for mark in VIDEO_MARKS)


def start(db_path, data_dir, user_id: int, wanted: str) -> tuple[str, str]:
    """Fetch what `wanted` points at and queue a run for it. Returns (run_id, what it did)."""
    wanted = (wanted or "").strip()
    if not wanted:
        raise sources.SourceError("give a video or channel address")
    cap = limits()
    if runs_started_today(db_path, user_id) >= cap["runs_per_day"]:
        raise sources.SourceError("you have used today's %d runs" % cap["runs_per_day"])

    local = Path(wanted)
    rights: dict = {}
    if local.is_file():
        url, note, saved = None, "from the file you gave", local
    elif looks_like_a_video(wanted):
        url, note, saved = wanted, "from that video", None
    else:
        channel = sources.resolve(wanted)
        uploads = sources.feed(channel.channel_id)
        if not uploads:
            raise sources.SourceError("%s has nothing in its feed" % channel.title)
        url, note = uploads[0].url, "the newest upload on %s: %s" % (channel.title, uploads[0].title)
        saved = None
        known = next(
            (row for row in channels.listed(db_path, user_id) if row["channel_id"] == channel.channel_id),
            None,
        )
        # a channel already on file keeps its record; one that is not stays a draft-only source
        rights = channels.rights_of(known) if known else {
            "basis": channels.NONE, "channel": channel.title, "channel_id": channel.channel_id,
        }

    run_id = secrets.token_urlsafe(9)
    folder = Path(data_dir) / "runs" / run_id
    if saved is None:
        saved = sources.fetch(url, folder, cap["max_minutes"])
    probe = media.probe(str(saved))
    if probe.audio_tracks == 0:
        raise sources.SourceError("that video has no sound to cut from")

    raw = settings.run_config(db_path, user_id, Path(saved).resolve(), (folder / "out").resolve(), rights)
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """INSERT INTO runs (id, user_id, status, source_name, source_path, source_bytes,
                   probe_json, config_json, queued_at)
                   VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, datetime('now'))""",
                (run_id, user_id, Path(saved).name, str(saved), Path(saved).stat().st_size,
                 json.dumps(probe.__dict__), json.dumps(raw)),
            )
    finally:
        conn.close()
    return run_id, note


def register(app, page) -> None:
    @app.post("/everygen")
    async def everygen(request: Request):
        user = request.state.user
        form = await request.form()
        try:
            run_id, _ = start(
                app.state.db_path, app.state.data_dir, user["id"], str(form.get("url", ""))
            )
        except sources.SourceError as exc:
            return RedirectResponse("/runs?error=%s" % _code(str(exc)), status_code=303)
        except media.MediaError:
            return RedirectResponse("/runs?error=unreadable", status_code=303)
        return RedirectResponse("/runs/%s" % run_id, status_code=303)


def _code(message: str) -> str:
    lowered = message.lower()
    if "today" in lowered:
        return "cap"
    if "refused" in lowered or "cookies" in lowered:
        return "refused"
    if "feed" in lowered or "channel" in lowered or "address" in lowered:
        return "not_found"
    return "failed"
