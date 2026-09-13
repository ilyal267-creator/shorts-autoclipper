"""The worker: takes queued runs one at a time and runs the pipeline on them.

One at a time on purpose. Transcribing and rendering use every core; two runs side by side
finish no sooner than one after the other, and both testers wait longer to see anything.
"""

from __future__ import annotations

import json
import time
import traceback

from .. import config as config_mod, run as run_mod
from . import db

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


def work(db_path, poll_seconds: float = 2.0, once: bool = False) -> None:
    db.migrate(db_path)
    recovered = recover(db_path)
    if recovered:
        print("marked %d interrupted run(s) failed" % recovered, flush=True)
    while True:
        run = claim_next(db_path)
        if run is None:
            if once:
                return
            time.sleep(poll_seconds)
            continue
        print("run %s started" % run["id"], flush=True)
        print("run %s finished: %s" % (run["id"], execute(db_path, run)), flush=True)
