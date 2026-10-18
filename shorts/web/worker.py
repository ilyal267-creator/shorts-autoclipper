"""The worker: takes queued runs one at a time and runs the pipeline on them.

One at a time on purpose. Transcribing and rendering use every core; two runs side by side
finish no sooner than one after the other, and both testers wait longer to see anything.
"""

from __future__ import annotations

import json
import secrets
import time
import traceback
from pathlib import Path

from .. import config as config_mod, media, run as run_mod, sources
from . import channels, connections, db, posting
from .runs import limits, runs_started_today

STAGES = ["probe", "transcribe", "plan", "copy", "compliance", "voiceover", "render", "deliver"]


class Cancelled(BaseException):
    """BaseException, not Exception: the pipeline deliberately survives any Exception in one
    clip, and a cancel has to get past that to stop the whole run."""


def recover(db_path) -> int:
    """A run left 'running' belongs to a worker that died; say so instead of spinning forever."""
    conn = db.connect(db_path)
    try:
        with conn:
            return conn.execute(
                """UPDATE runs SET status = 'failed', finished_at = datetime('now'),
                   error = 'The server restarted while this run was going. Start a new run to try again.'
                   WHERE status = 'running'"""
            ).rowcount
    finally:
        conn.close()


def claim_next(db_path):
    conn = db.connect(db_path)
    try:
        with conn:
            return conn.execute(
                """UPDATE runs SET status = 'running', started_at = datetime('now')
                   WHERE id = (SELECT id FROM runs WHERE status = 'queued' ORDER BY queued_at, id LIMIT 1)
                     AND status = 'queued'
                   RETURNING *"""
            ).fetchone()
    finally:
        conn.close()


def outcome(summary: dict) -> str:
    if summary.get("error") or summary.get("halted"):
        return "failed"
    if not summary.get("clips"):
        return "no_clips"
    return "needs_review"


def execute(db_path, run) -> str:
    """Run one claimed run to its end; returns the status it finished with."""
    conn = db.connect(db_path)

    def on_event(stage, message, data):
        with conn:
            conn.execute(
                "INSERT INTO run_events (run_id, stage, message, data_json) VALUES (?, ?, ?, ?)",
                (run["id"], stage, message, json.dumps(data) if data is not None else None),
            )
            if stage:
                conn.execute("UPDATE runs SET stage = ? WHERE id = ?", (stage, run["id"]))
        asked = conn.execute("SELECT cancel_requested_at FROM runs WHERE id = ?", (run["id"],)).fetchone()
        if asked and asked[0]:
            raise Cancelled()

    try:
        cfg = config_mod.from_dict(json.loads(run["config_json"]))
        summary = run_mod.execute(cfg, on_event=on_event)
        status, error = outcome(summary), summary.get("error") or summary.get("halted")
    except Cancelled:
        summary, status, error = None, "cancelled", None
    except Exception as exc:  # a broken config or a bug: record it, keep the worker alive
        summary, status, error = None, "failed", "%s: %s" % (type(exc).__name__, exc)
        traceback.print_exc()

    try:
        with conn:
            conn.execute(
                """UPDATE runs SET status = ?, error = ?, summary_json = ?, finished_at = datetime('now')
                   WHERE id = ?""",
                (status, error, json.dumps(summary, ensure_ascii=False) if summary else None, run["id"]),
            )
    finally:
        conn.close()
    return status


def queue_from_channel(db_path, data_dir, source) -> str | None:
    """Fetch what is new on one channel and queue a run for it. None when there is nothing."""
    cap = limits()
    if runs_started_today(db_path, source["user_id"]) >= cap["runs_per_day"]:
        channels.checked(db_path, source["id"])  # not a fault of the channel: look again later
        return None

    uploads = sources.feed(source["channel_id"])
    fresh = sources.new_since(uploads, source["last_video_id"])
    if not fresh:
        channels.checked(db_path, source["id"])
        return None

    video = fresh[0]  # oldest unseen first, one per check, so a backlog arrives in order
    run_id = secrets.token_urlsafe(9)
    folder = Path(data_dir) / "runs" / run_id
    saved = sources.fetch(video.url, folder, cap["max_minutes"])
    probe = media.probe(str(saved))
    if probe.audio_tracks == 0:
        raise sources.SourceError("that upload has no audio track to cut from")

    platforms = sorted({
        row["platform"] for row in connections.listed(db_path, source["user_id"])
        if row["status"] == "ok"
    }) or list(config_mod.PLATFORMS)
    raw = {
        "source_video": str(saved.resolve()),
        "connected_accounts": [
            {"platform": platform, "account_id": "pending", "handle": ""} for platform in platforms
        ],
        "clip_count": 3,
        "clip_length_range": [15, 45],
        # Automatic publishing is decided per clip once it exists (T22); a fetched upload
        # always becomes drafts first, whatever the channel is allowed to do.
        "posting_mode": "draft_for_approval",
        "rights_confirmed": True,
        "rights": channels.rights_of(source),
        "voiceover": {"enabled": False, "disclose": True},
        "output_dir": str((folder / "out").resolve()),
    }
    config_mod.from_dict(raw)  # refuse here rather than when the worker picks it up

    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """INSERT INTO runs (id, user_id, status, source_name, source_path, source_bytes,
                   probe_json, config_json, queued_at, source_id)
                   VALUES (?, ?, 'queued', ?, ?, ?, ?, ?, datetime('now'), ?)""",
                (
                    run_id, source["user_id"], video.title[:120] or video.video_id, str(saved),
                    saved.stat().st_size, json.dumps(probe.__dict__), json.dumps(raw), source["id"],
                ),
            )
    finally:
        conn.close()
    channels.checked(db_path, source["id"], video.video_id)
    return run_id


def check_channels(db_path, data_dir, every_minutes: int = 60) -> int:
    """One pass over the channels that are due. Returns how many runs it queued."""
    queued = 0
    for source in channels.due(db_path, every_minutes):
        try:
            run_id = queue_from_channel(db_path, data_dir, source)
        except sources.SourceError as exc:
            # one channel's trouble is that channel's trouble, not the worker's
            channels.trouble(db_path, source["id"], str(exc), exc.pause)
            print("channel %s: %s" % (source["channel_id"], exc), flush=True)
            continue
        except Exception as exc:
            channels.trouble(db_path, source["id"], "%s: %s" % (type(exc).__name__, exc), False)
            traceback.print_exc()
            continue
        if run_id:
            queued += 1
            print("channel %s queued run %s" % (source["channel_id"], run_id), flush=True)
    return queued


def post_what_we_may(db_path, data_dir, run_id: str) -> list[str]:
    """Auto-publish for a finished run, as one readable line per draft."""
    conn = db.connect(db_path)
    try:
        run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    finally:
        conn.close()
    lines = []
    try:
        outcomes = posting.auto_publish(db_path, data_dir, run)
    except Exception as exc:  # posting trouble is never the run's trouble
        traceback.print_exc()
        return ["auto-publish failed: %s" % exc]
    for outcome in outcomes:
        where = "%s %s" % (outcome["clip_id"], outcome["platform"])
        if outcome.get("held"):
            lines.append("%s held back: %s" % (where, outcome["held"]))
        elif outcome.get("status") == "published":
            lines.append("%s posted: %s" % (where, outcome.get("post_id")))
        else:
            lines.append("%s did not post: %s" % (where, outcome.get("error")))
    return lines


def work(db_path, poll_seconds: float = 2.0, once: bool = False, data_dir=None,
         every_minutes: int = 60) -> None:
    db.migrate(db_path)
    recovered = recover(db_path)
    if recovered:
        print("marked %d interrupted run(s) failed" % recovered, flush=True)
    while True:
        run = claim_next(db_path)
        if run is None:
            # nothing waiting: this is when there is time to see what the channels have
            if data_dir is not None and check_channels(db_path, data_dir, every_minutes):
                continue
            if once:
                return
            time.sleep(poll_seconds)
            continue
        print("run %s started" % run["id"], flush=True)
        status = execute(db_path, run)
        print("run %s finished: %s" % (run["id"], status), flush=True)
        if status == "needs_review" and data_dir is not None:
            for post in post_what_we_may(db_path, data_dir, run["id"]):
                print("  %s" % post, flush=True)
