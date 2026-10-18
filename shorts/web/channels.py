"""Partner channels: the ones this tester may clip, and what says so.

A channel earns its place one of two ways. Either it is **yours** — its id matches a YouTube
account you connected, so the server checks it rather than taking your word — or the owner
**gave you permission**, which means a person's name, a date and where the permission is kept.
Anything else is watched on sufferance: it can be clipped into drafts a human looks at, and it
can never post by itself.

That is the whole difference between a clipping partnership and reposting someone's work.
"""

from __future__ import annotations

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import sources
from . import connections, db

OWNED, PERMITTED, NONE = "owned", "permitted", "none"
MAY_AUTO_PUBLISH = (OWNED, PERMITTED)
RIGHTS_LABELS = {
    OWNED: "Your own channel",
    PERMITTED: "Permission from the owner",
    NONE: "No rights on file",
}
ERRORS = {
    "none_found": "None of those looked like a YouTube channel.",
    "permission": "Permission needs a name, a date and where it is kept.",
    "auto_blocked": "Only a channel you own, or one with permission on file, can post by itself.",
    "unreachable": "YouTube could not be reached just now. Try again in a minute.",
}


def owned_channel_ids(db_path, user_id: int) -> set[str]:
    """The channels this tester has actually connected - the only ones 'owned' can mean."""
    return {
        row["account_id"]
        for row in connections.listed(db_path, user_id, "youtube_shorts")
    }


def listed(db_path, user_id: int) -> list:
    conn = db.connect(db_path)
    try:
        return conn.execute(
            "SELECT * FROM channel_sources WHERE user_id = ? ORDER BY created_at DESC, id DESC",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()


def owned_source(db_path, source_id: int, user_id: int):
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM channel_sources WHERE id = ? AND user_id = ?", (source_id, user_id)
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(404)
    return row


def by_id(db_path, source_id: int):
    conn = db.connect(db_path)
    try:
        return conn.execute("SELECT * FROM channel_sources WHERE id = ?", (source_id,)).fetchone()
    finally:
        conn.close()


def rights_of(row) -> dict:
    """What travels with every run from this channel, and into its summary."""
    return {
        "basis": row["rights"],
        "channel": row["title"] or row["channel_id"],
        "channel_id": row["channel_id"],
        "granted_by": row["granted_by"],
        "granted_at": row["granted_at"],
        "evidence": row["evidence"],
        "attribution": row["attribution"],
        "may_auto_publish": bool(row["auto_publish"]) and row["rights"] in MAY_AUTO_PUBLISH,
    }


def add(db_path, user_id: int, channel, rights: str, permission: dict) -> int:
    conn = db.connect(db_path)
    try:
        with conn:
            existing = conn.execute(
                "SELECT id FROM channel_sources WHERE user_id = ? AND channel_id = ?",
                (user_id, channel.channel_id),
            ).fetchone()
            values = (
                channel.title, rights, permission.get("granted_by"), permission.get("granted_at"),
                permission.get("evidence"), permission.get("rules"), permission.get("attribution"),
            )
            if existing:
                conn.execute(
                    """UPDATE channel_sources SET title = ?, rights = ?, granted_by = ?, granted_at = ?,
                       evidence = ?, rules = ?, attribution = ? WHERE id = ?""",
                    values + (existing["id"],),
                )
                return existing["id"]
            cursor = conn.execute(
                """INSERT INTO channel_sources (user_id, channel_id, url, title, rights, granted_by,
                   granted_at, evidence, rules, attribution)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (user_id, channel.channel_id, channel.url) + values,
            )
            return cursor.lastrowid
    finally:
        conn.close()


CADENCES = {60: "Hourly", 360: "Every 6 hours", 1440: "Daily", 10080: "Weekly"}


def due(db_path, every_minutes: int | None = None) -> list:
    """Channels whose own cadence says it is time, longest-waiting first.

    `every_minutes` overrides every channel's cadence, which is only for tests and for a
    worker told to sweep everything now."""
    conn = db.connect(db_path)
    try:
        return conn.execute(
            """SELECT * FROM channel_sources
               WHERE active = 1
                 AND (last_checked_at IS NULL
                      OR last_checked_at <= datetime('now', '-' || ? || ' minutes')
                      OR last_checked_at <= datetime('now', '-' || cadence_minutes || ' minutes'))
               ORDER BY last_checked_at IS NOT NULL, last_checked_at, id
               LIMIT 20""",
            (every_minutes if every_minutes is not None else 10 ** 6,),
        ).fetchall()
    finally:
        conn.close()


def checked(db_path, source_id: int, last_video_id: str | None = None) -> None:
    conn = db.connect(db_path)
    try:
        with conn:
            if last_video_id:
                conn.execute(
                    """UPDATE channel_sources SET last_checked_at = datetime('now'),
                       last_video_id = ?, paused_reason = NULL WHERE id = ?""",
                    (last_video_id, source_id),
                )
            else:
                conn.execute(
                    "UPDATE channel_sources SET last_checked_at = datetime('now') WHERE id = ?",
                    (source_id,),
                )
    finally:
        conn.close()


def trouble(db_path, source_id: int, reason: str, pause: bool) -> None:
    """Say what went wrong on the card, and stop checking when YouTube has refused us."""
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                """UPDATE channel_sources SET paused_reason = ?, active = ?,
                   last_checked_at = datetime('now') WHERE id = ?""",
                (reason[:300], 0 if pause else 1, source_id),
            )
    finally:
        conn.close()


def register(app, page) -> None:
    @app.get("/channels")
    def channels(request: Request, error: str = ""):
        user = request.state.user
        mine = owned_channel_ids(app.state.db_path, user["id"])
        rows = []
        for row in listed(app.state.db_path, user["id"]):
            record = rights_of(row)
            rows.append({
                **record,
                "id": row["id"],
                "url": row["url"],
                "rights_label": RIGHTS_LABELS[row["rights"]],
                "auto_publish": bool(row["auto_publish"]),
                "may_auto": row["rights"] in MAY_AUTO_PUBLISH,
                "active": bool(row["active"]),
                "paused_reason": row["paused_reason"],
                "rules": row["rules"],
                "last_checked_at": row["last_checked_at"],
                "cadence_minutes": row["cadence_minutes"],
                "cadence": CADENCES.get(row["cadence_minutes"], "%d minutes" % row["cadence_minutes"]),
            })
        return page(
            request, "channels.html", nav="channels", channels=rows,
            connected=len(mine), error=ERRORS.get(error), cadences=CADENCES,
        )

    @app.post("/channels")
    async def add_channels(request: Request):
        """One URL or twenty, one per line, sharing one rights record."""
        user = request.state.user
        form = await request.form()
        wanted = [line.strip() for line in str(form.get("urls", "")).splitlines() if line.strip()]
        permission = {
            "granted_by": str(form.get("granted_by", "")).strip(),
            "granted_at": str(form.get("granted_at", "")).strip(),
            "evidence": str(form.get("evidence", "")).strip(),
            "rules": str(form.get("rules", "")).strip(),
            "attribution": str(form.get("attribution", "")).strip(),
        }
        claims_permission = form.get("has_permission") == "yes"
        if claims_permission and not all(
            permission[field] for field in ("granted_by", "granted_at", "evidence")
        ):
            return RedirectResponse("/channels?error=permission", status_code=303)

        mine = owned_channel_ids(app.state.db_path, user["id"])
        added = 0
        for url in wanted:
            try:
                channel = sources.resolve(url)
            except sources.SourceError:
                continue
            # ownership is checked against a connected account; permission is a person's word,
            # recorded as such; everything else gets nothing and stays in drafts
            if channel.channel_id in mine:
                basis, record = OWNED, {"attribution": permission["attribution"], "rules": permission["rules"]}
            elif claims_permission:
                basis, record = PERMITTED, permission
            else:
                basis, record = NONE, {"rules": permission["rules"], "attribution": permission["attribution"]}
            add(app.state.db_path, user["id"], channel, basis, record)
            added += 1
        if not added:
            return RedirectResponse("/channels?error=none_found", status_code=303)
        return RedirectResponse("/channels", status_code=303)

    @app.post("/channels/{source_id}/auto")
    async def set_auto(request: Request, source_id: int):
        row = owned_source(app.state.db_path, source_id, request.state.user["id"])
        form = await request.form()
        wanted = form.get("auto_publish") == "yes"
        # the gate, enforced here rather than by the form that drew the switch
        if wanted and row["rights"] not in MAY_AUTO_PUBLISH:
            return RedirectResponse("/channels?error=auto_blocked", status_code=303)
        conn = db.connect(app.state.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE channel_sources SET auto_publish = ? WHERE id = ?", (1 if wanted else 0, source_id)
                )
        finally:
            conn.close()
        return RedirectResponse("/channels", status_code=303)

    @app.post("/channels/{source_id}/cadence")
    async def set_cadence(request: Request, source_id: int):
        owned_source(app.state.db_path, source_id, request.state.user["id"])
        form = await request.form()
        try:
            minutes = int(form.get("cadence_minutes", 1440))
        except ValueError:
            return RedirectResponse("/channels", status_code=303)
        if minutes not in CADENCES:
            return RedirectResponse("/channels", status_code=303)
        conn = db.connect(app.state.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE channel_sources SET cadence_minutes = ? WHERE id = ?",
                    (minutes, source_id),
                )
        finally:
            conn.close()
        return RedirectResponse("/channels", status_code=303)

    @app.post("/channels/{source_id}/pause")
    def pause(request: Request, source_id: int):
        row = owned_source(app.state.db_path, source_id, request.state.user["id"])
        conn = db.connect(app.state.db_path)
        try:
            with conn:
                conn.execute(
                    "UPDATE channel_sources SET active = ?, paused_reason = NULL WHERE id = ?",
                    (0 if row["active"] else 1, source_id),
                )
        finally:
            conn.close()
        return RedirectResponse("/channels", status_code=303)

    @app.post("/channels/{source_id}/remove")
    def remove(request: Request, source_id: int):
        owned_source(app.state.db_path, source_id, request.state.user["id"])
        conn = db.connect(app.state.db_path)
        try:
            with conn:
                conn.execute("DELETE FROM channel_sources WHERE id = ?", (source_id,))
        finally:
            conn.close()
        return RedirectResponse("/channels", status_code=303)
