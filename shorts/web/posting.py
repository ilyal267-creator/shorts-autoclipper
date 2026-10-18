"""Posting, and everything that is allowed to stop it.

One path for both ways a clip reaches a platform: a person pressing Publish, and a watched
channel posting by itself. They share the rails on purpose - a rule that only applies when
nobody is looking is not a rule.

The rails, in the order they are checked:
  1. the channel's rights record - ownership or permission, or it does not post by itself
  2. the tester's pause switch - one click stops everything, mid-run included
  3. anything compliance flagged - that clip waits for a person, whatever else is true
  4. the per-account daily cap - a bug that posts in a loop costs one day's cap, not an account
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone

from ..run import publish_draft
from . import channels, connections, db

POST_URLS = {"youtube_shorts": "https://youtu.be/%s"}


def cap() -> int:
    return int(os.getenv("SHORTS_POSTS_PER_ACCOUNT_PER_DAY", 5))


# ----------------------------------------------------------------- the pause switch


def paused(db_path, user_id: int) -> bool:
    conn = db.connect(db_path)
    try:
        row = conn.execute("SELECT posting_paused_at FROM users WHERE id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    return bool(row and row["posting_paused_at"])


def set_paused(db_path, user_id: int, stop: bool) -> None:
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE users SET posting_paused_at = %s WHERE id = ?"
                % ("datetime('now')" if stop else "NULL"),
                (user_id,),
            )
    finally:
        conn.close()


def posted_today(db_path, user_id: int, account_id: str) -> int:
    conn = db.connect(db_path)
    try:
        return conn.execute(
            """SELECT count(*) FROM clip_versions
               JOIN runs ON runs.id = clip_versions.run_id
               WHERE runs.user_id = ? AND clip_versions.account_id = ?
                 AND clip_versions.published_at >= date('now')""",
            (user_id, account_id),
        ).fetchone()[0]
    finally:
        conn.close()


# ----------------------------------------------------------------- publishing one draft


def record(db_path, run_id: str, clip_id: str, platform: str, result, account_id: str, by: str) -> None:
    from .runs import hide_paths

    conn = db.connect(db_path)
    try:
        with conn:
            if result.status == "published":
                conn.execute(
                    """UPDATE clip_versions SET state = 'published', post_id = ?, post_url = ?,
                       published_at = datetime('now'), publish_error = NULL, account_id = ?,
                       published_by = ?
                       WHERE run_id = ? AND clip_id = ? AND platform = ?""",
                    (result.post_id, POST_URLS.get(platform, "%s") % result.post_id, account_id, by,
                     run_id, clip_id, platform),
                )
            else:
                conn.execute(
                    """UPDATE clip_versions SET publish_error = ?
                       WHERE run_id = ? AND clip_id = ? AND platform = ?""",
                    (hide_paths(result.error or "that did not go through"), run_id, clip_id, platform),
                )
    finally:
        conn.close()


def send(db_path, data_dir, user_id: int, run_id: str, clip_id: str, platform: str, entry: dict,
         copy_row, by: str = "tester"):
    """Post one draft. Returns (result, account) or (None, None) when nothing is connected."""
    try:
        account, creds = connections.creds_for(db_path, data_dir, user_id, platform)
    except connections.ConnectError:
        account, creds = None, None
    if account is None or creds is None:
        return None, None

    entry = dict(entry)
    entry.update({
        "title": copy_row["title"],
        "caption": copy_row["caption"],
        "hashtags": json.loads(copy_row["hashtags_json"]),
    })
    result = publish_draft(
        entry, platform, account["account_id"], creds, dry_run=os.getenv("SHORTS_DRY_RUN") == "1"
    )
    record(db_path, run_id, clip_id, platform, result, account["account_id"], by)
    if result.status != "published" and "no %s account" % platform.split("_")[0] in (result.error or ""):
        connections.needs_reconnect(db_path, account["id"])
    return result, account


# ----------------------------------------------------------------- posting by itself


def held_back(db_path, user_id: int, source, flags: list[str], account_id: str | None) -> str | None:
    """Why this clip must wait for a person, or None when it may go."""
    if source is None or not source["auto_publish"]:
        return None if source is None else "this channel is not set to post by itself"
    if source["rights"] not in channels.MAY_AUTO_PUBLISH:
        return "no rights record for this channel"
    if paused(db_path, user_id):
        return "posting is paused"
    if flags:
        return "compliance flagged it: %s" % flags[0]
    if account_id and posted_today(db_path, user_id, account_id) >= cap():
        return "today's limit of %d posts for that account is used up" % cap()
    return None


def auto_publish(db_path, data_dir, run) -> list[dict]:
    """Post what this finished run is allowed to post. Everything else stays a draft."""
    from .review import ensure_versions, flags_for, versions

    if not run["source_id"] or not run["summary_json"]:
        return []
    source = channels.by_id(db_path, run["source_id"])
    if source is None or not source["auto_publish"] or source["rights"] not in channels.MAY_AUTO_PUBLISH:
        return []

    summary = json.loads(run["summary_json"])
    ensure_versions(db_path, run["id"], summary)
    saved = versions(db_path, run["id"])
    _, per_clip = flags_for(summary)
    connected = {
        row["platform"]: row for row in connections.listed(db_path, run["user_id"]) if row["status"] == "ok"
    }

    done = []
    for clip in summary.get("clips", []):
        for platform, entry in (clip.get("platforms") or {}).items():
            if entry.get("status") != "draft" or platform not in connected:
                continue
            row = saved.get((clip["clip_id"], platform))
            if row is None or row["state"] == "published":
                continue
            flags = per_clip.get((clip["clip_id"], None), []) + per_clip.get((clip["clip_id"], platform), [])
            # the pause is read again for every single clip, so one click stops a run mid-flight
            reason = held_back(db_path, run["user_id"], source, flags, connected[platform]["account_id"])
            if reason:
                done.append({"clip_id": clip["clip_id"], "platform": platform, "held": reason})
                continue
            result, account = send(
                db_path, data_dir, run["user_id"], run["id"], clip["clip_id"], platform, entry, row, by="auto"
            )
            if result is None:
                done.append({"clip_id": clip["clip_id"], "platform": platform, "held": "not connected"})
                continue
            done.append({
                "clip_id": clip["clip_id"], "platform": platform,
                "status": result.status, "post_id": result.post_id, "error": result.error,
            })
    return done


# ----------------------------------------------------------------- the digest


def recent(db_path, user_id: int, days: int = 7) -> list[dict]:
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            """SELECT clip_versions.*, runs.source_name, runs.id AS run_id,
                      channel_sources.title AS channel, connections.handle AS account
               FROM clip_versions
               JOIN runs ON runs.id = clip_versions.run_id
               LEFT JOIN channel_sources ON channel_sources.id = runs.source_id
               LEFT JOIN connections ON connections.account_id = clip_versions.account_id
                    AND connections.user_id = runs.user_id
               WHERE runs.user_id = ? AND clip_versions.published_at >= datetime('now', ?)
               ORDER BY clip_versions.published_at DESC""",
            (user_id, "-%d days" % days),
        ).fetchall()
    finally:
        conn.close()
    return [
        {
            "when": row["published_at"],
            "platform": row["platform"],
            "account": row["account"] or row["account_id"] or "",
            "clip": "%s · %s" % (row["clip_id"].replace("clip_", "Clip "), row["source_name"]),
            "channel": row["channel"],
            "caption": (row["caption"] or "")[:120],
            "url": row["post_url"],
            "run_id": row["run_id"],
            "automatic": row["published_by"] == "auto",
        }
        for row in rows
    ]


def counts(db_path, user_id: int) -> dict:
    """What the digest header says: today, this week, and what each account has left."""
    posts = recent(db_path, user_id, days=7)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return {
        "today": sum(1 for post in posts if (post["when"] or "").startswith(today)),
        "week": len(posts),
        "automatic": sum(1 for post in posts if post["automatic"]),
        "cap": cap(),
    }
