"""Review: watch each platform's version of each clip, fix its copy, approve it."""

from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from . import db
from .runs import PLATFORM_NAMES, VOICE_NAMES, duration_text, hide_paths, owned_run

# What each platform accepts: longer copy is cut off or refused when it posts.
LIMITS = {
    "tiktok": {"caption": 2200, "hashtags": 10},
    "instagram_reels": {"caption": 2200, "hashtags": 30},
    "youtube_shorts": {"title": 100, "caption": 4900, "hashtags": 15},
}
EDIT_ERRORS = {
    "hashtags": "Hashtags are words or #words separated by spaces, with no punctuation inside them.",
    "too_long": "That's more than this platform allows. Shorten the caption or use fewer hashtags.",
    "title": "A YouTube title needs 1 to 100 characters.",
}
TAG = re.compile(r"^#?[\wÀ-￿]{1,60}$")


def parse_hashtags(text: str) -> list[str] | None:
    """'#coding devtok, #shorts' -> ['#coding', '#devtok', '#shorts']; None if any isn't a tag."""
    tags = [part for part in re.split(r"[\s,]+", text.strip()) if part]
    if not all(TAG.match(tag) for tag in tags):
        return None
    seen, out = set(), []
    for tag in tags:
        tag = "#" + tag.lstrip("#")
        if tag.lower() not in seen:
            seen.add(tag.lower())
            out.append(tag)
    return out


def summary_of(run) -> dict:
    if run["status"] != "needs_review" or not run["summary_json"]:
        raise HTTPException(409, "this run has no drafts to review")
    return json.loads(run["summary_json"])


def ensure_versions(db_path, run_id: str, summary: dict) -> None:
    """One row per draft, seeded from what the model wrote; later edits live in the row."""
    conn = db.connect(db_path)
    try:
        with conn:
            for clip in summary.get("clips", []):
                for platform, entry in (clip.get("platforms") or {}).items():
                    if entry.get("status") != "draft":
                        continue
                    conn.execute(
                        """INSERT OR IGNORE INTO clip_versions (run_id, clip_id, platform, title, caption, hashtags_json)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (run_id, clip["clip_id"], platform, entry.get("title"), entry.get("caption") or "",
                         json.dumps(entry.get("hashtags") or [])),
                    )
    finally:
        conn.close()


def versions(db_path, run_id: str) -> dict:
    conn = db.connect(db_path)
    try:
        rows = conn.execute("SELECT * FROM clip_versions WHERE run_id = ?", (run_id,)).fetchall()
    finally:
        conn.close()
    return {(r["clip_id"], r["platform"]): r for r in rows}


def flags_for(summary: dict) -> tuple[list[str], dict]:
    """Run-wide flags, and per-clip flags keyed by (clip_id, platform or None)."""
    general, per_clip = [], {}
    for flag in summary.get("flags_for_human_review", []):
        match = re.match(r"(clip_\d+)\b[:\s]+(.*)", flag)
        if not match:
            general.append(hide_paths(flag))
            continue
        clip_id, text = match.groups()
        platform = next((p for p in PLATFORM_NAMES if p in text), None)
        per_clip.setdefault((clip_id, platform), []).append(hide_paths(text))
    return general, per_clip


def register(app, page) -> None:
    def load(request: Request, run_id: str):
        run = owned_run(app.state.db_path, run_id, request.state.user["id"])
        return run, summary_of(run)

    def review_page(request: Request, run, clip: str = "", error: str = ""):
        summary = summary_of(run)
        ensure_versions(app.state.db_path, run["id"], summary)
        saved = versions(app.state.db_path, run["id"])
        general, per_clip = flags_for(summary)
        voices = summary.get("voices") or {}

        clips = []
        for c in summary.get("clips", []):
            cards = []
            for platform, entry in (c.get("platforms") or {}).items():
                row = saved.get((c["clip_id"], platform))
                flags = per_clip.get((c["clip_id"], None), []) + per_clip.get((c["clip_id"], platform), [])
                cards.append({
                    "platform": platform,
                    "name": PLATFORM_NAMES.get(platform, platform),
                    "status": entry.get("status"),
                    "reason": hide_paths(entry.get("reason") or entry.get("error") or ""),
                    "voice": VOICE_NAMES.get(voices.get(platform), "") if voices.get(platform) else "",
                    "video": "/runs/%s/media/%s" % (run["id"], Path(entry["asset_ref"]).name) if entry.get("asset_ref") else None,
                    "hook": entry.get("hook"),
                    "title": row["title"] if row else entry.get("title"),
                    "caption": row["caption"] if row else entry.get("caption"),
                    "hashtags": json.loads(row["hashtags_json"]) if row else entry.get("hashtags") or [],
                    "approved": bool(row and row["state"] == "approved"),
                    "edited": bool(row and row["edited_at"]),
                    "synthetic": entry.get("synthetic_media"),
                    "flags": flags,
                    "limits": LIMITS.get(platform, {}),
                })
            clips.append({
                "id": c["clip_id"],
                "label": "Clip %s" % c["clip_id"].split("_")[-1],
                "length": "%.1fs" % (c.get("duration") or (c["end"] - c["start"])),
                "span": "%s–%s" % (duration_text(c["start"]), duration_text(c["end"])),
                "reason": c.get("selection_reason", ""),
                "error": hide_paths(c.get("error") or ""),
                "cards": cards,
            })
        if not clips:
            raise HTTPException(409, "this run has no clips")
        current = next((c for c in clips if c["id"] == clip), clips[0])
        drafts = [card for c in clips for card in c["cards"] if card["status"] == "draft"]
        approved = sum(1 for card in drafts if card["approved"])
        return page(
            request, "review.html", nav="runs", run=run, clips=clips, clip=current, general_flags=general,
            approved=approved, drafts=len(drafts), error=EDIT_ERRORS.get(error),
        )

    app.state.review_page = review_page

    @app.get("/runs/{run_id}/media/{name}")
    def media(request: Request, run_id: str, name: str):
        run, summary = load(request, run_id)
        # only files this run rendered, looked up by name: never a path the request made up
        assets = {
            Path(ref).name: Path(ref)
            for clip in summary.get("clips", [])
            for ref in [clip.get("asset_ref")] + [e.get("asset_ref") for e in (clip.get("platforms") or {}).values()]
            if ref
        }
        path = assets.get(name)
        if path is None or not path.is_file():
            raise HTTPException(404)
        return FileResponse(path, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})

    def draft_or_404(run_id: str, clip_id: str, platform: str, summary: dict):
        for clip in summary.get("clips", []):
            entry = (clip.get("platforms") or {}).get(platform)
            if clip["clip_id"] == clip_id and entry and entry.get("status") == "draft":
                return entry
        raise HTTPException(404)

    def back(run_id: str, clip_id: str, platform: str, error: str = ""):
        suffix = "&error=%s" % error if error else ""
        return RedirectResponse("/runs/%s?clip=%s%s#%s" % (run_id, clip_id, suffix, platform), status_code=303)

    @app.post("/runs/{run_id}/clips/{clip_id}/{platform}/approve")
    def approve(request: Request, run_id: str, clip_id: str, platform: str):
        run, summary = load(request, run_id)
        draft_or_404(run_id, clip_id, platform, summary)
        ensure_versions(app.state.db_path, run_id, summary)
        conn = db.connect(app.state.db_path)
        try:
            with conn:
                conn.execute(
                    """UPDATE clip_versions SET state = 'approved', approved_at = datetime('now')
                       WHERE run_id = ? AND clip_id = ? AND platform = ? AND state = 'draft'""",
                    (run_id, clip_id, platform),
                )
        finally:
            conn.close()
        return back(run_id, clip_id, platform)

    @app.post("/runs/{run_id}/clips/{clip_id}/{platform}/unapprove")
    def unapprove(request: Request, run_id: str, clip_id: str, platform: str):
        run, summary = load(request, run_id)
        draft_or_404(run_id, clip_id, platform, summary)
        conn = db.connect(app.state.db_path)
        try:
            with conn:
                conn.execute(
                    """UPDATE clip_versions SET state = 'draft', approved_at = NULL
                       WHERE run_id = ? AND clip_id = ? AND platform = ? AND state = 'approved'""",
                    (run_id, clip_id, platform),
                )
        finally:
            conn.close()
        return back(run_id, clip_id, platform)

    @app.post("/runs/{run_id}/approve-all")
    def approve_all(request: Request, run_id: str):
        run, summary = load(request, run_id)
        ensure_versions(app.state.db_path, run_id, summary)
        conn = db.connect(app.state.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE clip_versions SET state = 'approved', approved_at = datetime('now') WHERE run_id = ? AND state = 'draft'",
                    (run_id,),
                )
        finally:
            conn.close()
        return RedirectResponse("/runs/%s" % run_id, status_code=303)

    @app.post("/runs/{run_id}/clips/{clip_id}/{platform}/copy")
    async def edit_copy(request: Request, run_id: str, clip_id: str, platform: str):
        run, summary = load(request, run_id)
        draft_or_404(run_id, clip_id, platform, summary)
        ensure_versions(app.state.db_path, run_id, summary)
        form = await request.form()
        limits = LIMITS.get(platform, {})
        caption = str(form.get("caption", "")).strip()
        tags = parse_hashtags(str(form.get("hashtags", "")))
        title = str(form.get("title", "")).strip() if "title" in limits else None
        if tags is None:
            return back(run_id, clip_id, platform, "hashtags")
        if len(caption) > limits.get("caption", 2200) or len(tags) > limits.get("hashtags", 30):
            return back(run_id, clip_id, platform, "too_long")
        if title is not None and not 1 <= len(title) <= limits["title"]:
            return back(run_id, clip_id, platform, "title")
        conn = db.connect(app.state.db_path)
        try:
            with conn:
                # an edit sends the draft back for approval: what was approved is not what posts now
                conn.execute(
                    """UPDATE clip_versions SET caption = ?, hashtags_json = ?, title = COALESCE(?, title),
                       edited_at = datetime('now'), state = 'draft', approved_at = NULL
                       WHERE run_id = ? AND clip_id = ? AND platform = ?""",
                    (caption, json.dumps(tags), title, run_id, clip_id, platform),
                )
        finally:
            conn.close()
        return back(run_id, clip_id, platform)
