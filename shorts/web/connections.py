"""Connected accounts: the sign-in to a platform, and the token that posts as them.

Each row is one account, not one platform, so a tester can hold three TikToks or two channels
and a clip can go to all of them. The refresh token is sealed before it is stored and only
opened to mint an access token, which lives in memory and nowhere else.
"""

from __future__ import annotations

import os
import time
import urllib.parse

from .. import auth as pipeline_auth
from . import crypto, db
from .auth import google_client

YOUTUBE_SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload "
    # readonly is what lets us name the channel on the card, and later check that a watched
    # channel is one this tester actually owns
    "https://www.googleapis.com/auth/youtube.readonly"
)
CONSENT = "https://accounts.google.com/o/oauth2/v2/auth?"
CHANNEL_API = "https://www.googleapis.com/youtube/v3/channels"
_tokens: dict[int, tuple[str, float]] = {}  # connection id -> (access token, expires at)


class ConnectError(Exception):
    pass


# ----------------------------------------------------------------- the store


def save(db_path, data_dir, user_id: int, platform: str, account_id: str, handle: str, secret: dict) -> int:
    """Store (or re-store) one account. Reconnecting the same account refreshes it in place."""
    sealed = crypto.seal(data_dir, secret)
    conn = db.connect(db_path)
    try:
        with conn:
            row = conn.execute(
                "SELECT id FROM connections WHERE user_id = ? AND platform = ? AND account_id = ?",
                (user_id, platform, account_id),
            ).fetchone()
            if row:
                conn.execute(
                    """UPDATE connections SET handle = ?, secret = ?, status = 'ok',
                       updated_at = datetime('now') WHERE id = ?""",
                    (handle, sealed, row["id"]),
                )
                _tokens.pop(row["id"], None)
                return row["id"]
            cursor = conn.execute(
                """INSERT INTO connections (user_id, platform, account_id, handle, secret)
                   VALUES (?, ?, ?, ?, ?)""",
                (user_id, platform, account_id, handle, sealed),
            )
            return cursor.lastrowid
    finally:
        conn.close()


def listed(db_path, user_id: int, platform: str | None = None) -> list:
    conn = db.connect(db_path)
    try:
        if platform:
            return conn.execute(
                "SELECT * FROM connections WHERE user_id = ? AND platform = ? ORDER BY id",
                (user_id, platform),
            ).fetchall()
        return conn.execute(
            "SELECT * FROM connections WHERE user_id = ? ORDER BY platform, id", (user_id,)
        ).fetchall()
    finally:
        conn.close()


def first(db_path, user_id: int, platform: str):
    rows = listed(db_path, user_id, platform)
    return rows[0] if rows else None


def forget(db_path, user_id: int, connection_id: int) -> None:
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM connections WHERE id = ? AND user_id = ?", (connection_id, user_id))
    finally:
        conn.close()
    _tokens.pop(connection_id, None)


def needs_reconnect(db_path, connection_id: int) -> None:
    """The platform stopped accepting this token: say so on the card instead of failing silently."""
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute(
                "UPDATE connections SET status = 'reconnect', updated_at = datetime('now') WHERE id = ?",
                (connection_id,),
            )
    finally:
        conn.close()
    _tokens.pop(connection_id, None)


# ----------------------------------------------------------------- YouTube


def consent_url(redirect_uri: str, state: str, challenge: str) -> str:
    client = google_client()
    if not client:
        raise ConnectError("this server has no Google client configured")
    return CONSENT + urllib.parse.urlencode({
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": YOUTUBE_SCOPES,
        "access_type": "offline",
        # always re-consent: without it Google hands back no refresh token the second time,
        # and a connection with no refresh token is one that dies within the hour
        "prompt": "select_account consent",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    })


def exchange(code: str, verifier: str, redirect_uri: str) -> dict:
    client = google_client()
    if not client:
        raise ConnectError("this server has no Google client configured")
    try:
        tokens = pipeline_auth._post_form(
            "https://oauth2.googleapis.com/token",
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "code": code,
                "code_verifier": verifier,
                "grant_type": "authorization_code",
                "redirect_uri": redirect_uri,
            },
        )
    except pipeline_auth.AuthError as exc:
        raise ConnectError(str(exc)) from exc
    if not tokens.get("refresh_token"):
        raise ConnectError("Google sent no refresh token - disconnect the app and try again")
    return tokens


def channel_of(access_token: str) -> dict:
    """Whose channel this is. Also the ownership check a watched channel is measured against."""
    import requests

    response = requests.get(
        CHANNEL_API,
        params={"part": "snippet", "mine": "true"},
        headers={"Authorization": "Bearer %s" % access_token},
        timeout=30,
    )
    if response.status_code == 401:
        raise ConnectError("Google would not accept that token")
    if response.status_code >= 400:
        raise ConnectError("YouTube answered %d: %s" % (response.status_code, response.text[:200]))
    items = response.json().get("items") or []
    if not items:
        raise ConnectError("that Google account has no YouTube channel - make one, then connect again")
    return {"account_id": items[0]["id"], "handle": (items[0].get("snippet") or {}).get("title", "")}


def access_token(db_path, data_dir, row) -> str:
    """A live access token for this connection, minted from its sealed refresh token."""
    cached = _tokens.get(row["id"])
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    client = google_client()
    if not client:
        raise ConnectError("this server has no Google client configured")
    try:
        secret = crypto.open_sealed(data_dir, row["secret"])
    except KeyError as exc:
        needs_reconnect(db_path, row["id"])
        raise ConnectError(str(exc)) from exc
    try:
        tokens = pipeline_auth._post_form(
            "https://oauth2.googleapis.com/token",
            {
                "client_id": client["client_id"],
                "client_secret": client["client_secret"],
                "refresh_token": secret["refresh_token"],
                "grant_type": "refresh_token",
            },
        )
    except pipeline_auth.AuthError as exc:
        needs_reconnect(db_path, row["id"])  # revoked, or the password changed: ask for a reconnect
        raise ConnectError("YouTube no longer accepts this connection - connect it again") from exc
    token = tokens["access_token"]
    _tokens[row["id"]] = (token, time.time() + int(tokens.get("expires_in", 3600)))
    return token


def creds_for(db_path, data_dir, user_id: int, platform: str, account_id: str | None = None) -> tuple:
    """(row, credentials) for publishing, or (None, None) when nothing is connected."""
    rows = listed(db_path, user_id, platform)
    row = next((r for r in rows if account_id in (None, r["account_id"])), None)
    if row is None:
        return None, None
    if platform != "youtube_shorts":
        raise ConnectError("publishing to %s is not built yet" % platform)
    return row, {
        "access_token": access_token(db_path, data_dir, row),
        # unaudited projects may only upload privately, and a tester's channel is not a stage
        "privacy_status": os.getenv("YOUTUBE_PRIVACY_STATUS", "private"),
    }
