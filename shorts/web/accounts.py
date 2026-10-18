"""The Accounts page: connect a platform, see what is connected, disconnect it."""

from __future__ import annotations

import secrets

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse

from .. import auth as pipeline_auth
from ..voice import DEFAULT_VOICES
from . import connections, posting
from .runs import PLATFORM_NAMES, VOICE_NAMES

STATE_COOKIE = "connect_state"
CONNECT_ERRORS = {
    "failed": "That didn't finish. Try connecting again.",
    "refused": "Google didn't give us permission to upload. Tick the YouTube boxes on the consent screen.",
    "no_channel": "That Google account has no YouTube channel. Make one, then connect again.",
    "not_configured": "This server has no Google client set up yet.",
}
# TikTok and Instagram arrive in T9 and T10; the cards say so rather than pretending.
SOON = {
    "tiktok": "Connecting TikTok is next.",
    "instagram_reels": "Connecting Instagram is next. Reels post publicly, so it will ask twice.",
}


def register(app, page, public_url, secure_cookies) -> None:
    @app.get("/accounts")
    def accounts(request: Request, error: str = ""):
        user = request.state.user
        rows = connections.listed(app.state.db_path, user["id"])
        cards = []
        for platform, name in PLATFORM_NAMES.items():
            mine = [row for row in rows if row["platform"] == platform]
            cards.append({
                "platform": platform,
                "name": name,
                "accounts": [
                    {
                        "id": row["id"],
                        "handle": row["handle"] or row["account_id"],
                        "needs_reconnect": row["status"] == "reconnect",
                    }
                    for row in mine
                ],
                "connectable": platform == "youtube_shorts",
                "soon": SOON.get(platform),
                "voice": VOICE_NAMES.get(DEFAULT_VOICES[platform], ""),
            })
        return page(
            request, "accounts.html", nav="accounts", cards=cards,
            error=CONNECT_ERRORS.get(error),
            paused=posting.paused(app.state.db_path, user["id"]), cap=posting.cap(),
        )

    @app.get("/connect/youtube")
    def connect_youtube(request: Request):
        try:
            state = secrets.token_urlsafe(24)
            verifier, challenge = pipeline_auth.pkce_pair()
            url = connections.consent_url(public_url(request) + "/connect/youtube/callback", state, challenge)
        except connections.ConnectError:
            return RedirectResponse("/accounts?error=not_configured", status_code=303)
        response = RedirectResponse(url, status_code=303)
        response.set_cookie(
            STATE_COOKIE, state + "." + verifier, max_age=600, httponly=True,
            samesite="lax", secure=secure_cookies(request), path="/connect/youtube",
        )
        return response

    @app.get("/connect/youtube/callback")
    def connected_youtube(request: Request, code: str = "", state: str = "", error: str = "", scope: str = ""):
        user = request.state.user
        stored = request.cookies.get(STATE_COOKIE, "")
        expected, _, verifier = stored.partition(".")
        response = RedirectResponse("/accounts?error=failed", status_code=303)
        response.delete_cookie(STATE_COOKIE, path="/connect/youtube")
        if error or not code or not expected or not secrets.compare_digest(expected, state):
            return response
        if "youtube.upload" not in scope:
            response.headers["location"] = "/accounts?error=refused"
            return response
        try:
            tokens = connections.exchange(code, verifier, public_url(request) + "/connect/youtube/callback")
            channel = connections.channel_of(tokens["access_token"])
        except connections.ConnectError as exc:
            response.headers["location"] = "/accounts?error=%s" % ("no_channel" if "no YouTube channel" in str(exc) else "failed")
            return response
        connections.save(
            app.state.db_path, app.state.data_dir, user["id"], "youtube_shorts",
            channel["account_id"], channel["handle"], {"refresh_token": tokens["refresh_token"]},
        )
        response.headers["location"] = "/accounts"
        return response

    @app.get("/posts")
    def posts(request: Request):
        user = request.state.user
        return page(
            request, "posts.html", nav="posts",
            posts=posting.recent(app.state.db_path, user["id"]),
            counts=posting.counts(app.state.db_path, user["id"]),
            paused=posting.paused(app.state.db_path, user["id"]),
        )

    @app.post("/posting/pause")
    async def pause_posting(request: Request):
        """The one switch that stops everything, including a run already going."""
        form = await request.form()
        posting.set_paused(app.state.db_path, request.state.user["id"], form.get("stop") == "yes")
        return RedirectResponse(form.get("back") or "/posts", status_code=303)

    @app.post("/accounts/{connection_id}/disconnect")
    def disconnect(request: Request, connection_id: int):
        rows = connections.listed(app.state.db_path, request.state.user["id"])
        if not any(row["id"] == connection_id for row in rows):
            raise HTTPException(404)
        connections.forget(app.state.db_path, request.state.user["id"], connection_id)
        return RedirectResponse("/accounts", status_code=303)
