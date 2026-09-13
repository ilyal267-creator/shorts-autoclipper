"""Sign-in: Google OAuth for emails that were invited, and the sessions that follow.

Only a hash of each session token is stored, so a copy of the database signs nobody in. The ID
token comes straight from Google's token endpoint over TLS, in exchange for a code this server
asked for, so its claims are checked (audience, issuer, expiry, verified email) without fetching
Google's signing keys.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import auth as pipeline_auth
from . import db

AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URI = "https://oauth2.googleapis.com/token"
ISSUERS = {"https://accounts.google.com", "accounts.google.com"}
SESSION_COOKIE = "session"
STATE_COOKIE = "signin_state"
SESSION_DAYS = 14


class SignInError(Exception):
    pass


def google_client() -> dict | None:
    """The web OAuth client, from GOOGLE_CLIENT_ID/SECRET or the JSON Google Cloud downloads."""
    if os.getenv("GOOGLE_CLIENT_ID") and os.getenv("GOOGLE_CLIENT_SECRET"):
        return {"client_id": os.environ["GOOGLE_CLIENT_ID"], "client_secret": os.environ["GOOGLE_CLIENT_SECRET"]}
    path = Path(os.getenv("GOOGLE_SIGNIN_CLIENT_FILE", pipeline_auth.SECRETS_DIR / "google_signin_client.json"))
    if path.exists():
        web = json.loads(path.read_text(encoding="utf-8")).get("web") or {}
        if web.get("client_id") and web.get("client_secret"):
            return {"client_id": web["client_id"], "client_secret": web["client_secret"]}
    return None


def consent_url(client: dict, redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URI + "?" + urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": "openid email profile",
            "prompt": "select_account",
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
    )


def exchange(client: dict, code: str, verifier: str, redirect_uri: str) -> dict:
    """Trade the callback's code for the signed-in person's verified identity."""
    try:
        tokens = pipeline_auth._post_form(
            TOKEN_URI,
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
        raise SignInError(str(exc)) from exc
    claims = _claims(tokens.get("id_token", ""))
    if claims.get("aud") != client["client_id"] or claims.get("iss") not in ISSUERS:
        raise SignInError("the ID token was not issued for this app")
    if float(claims.get("exp", 0)) < time.time():
        raise SignInError("the ID token has expired")
    if not claims.get("email") or claims.get("email_verified") not in (True, "true"):
        raise SignInError("Google has not verified this account's email")
    return {"email": claims["email"], "name": claims.get("given_name") or claims.get("name") or ""}


def _claims(id_token: str) -> dict:
    try:
        payload = id_token.split(".")[1]
        return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError) as exc:
        raise SignInError("Google returned no usable ID token") from exc


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def start_session(db_path, email: str, name: str) -> str | None:
    """A new session token for an invited, unrevoked email; None for anyone else."""
    conn = db.connect(db_path)
    try:
        user = conn.execute(
            "SELECT id FROM users WHERE email = ? AND revoked_at IS NULL", (email,)
        ).fetchone()
        if not user:
            return None
        token = secrets.token_urlsafe(32)
        expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        with conn:
            conn.execute(
                "INSERT INTO sessions (token_hash, user_id, expires_at) VALUES (?, ?, ?)",
                (_hash(token), user["id"], expires.strftime("%Y-%m-%d %H:%M:%S")),
            )
            conn.execute(
                "UPDATE users SET name = COALESCE(NULLIF(?, ''), name), last_seen_at = datetime('now') WHERE id = ?",
                (name, user["id"]),
            )
        return token
    finally:
        conn.close()


def user_for(db_path, token: str | None) -> dict | None:
    if not token:
        return None
    conn = db.connect(db_path)
    try:
        row = conn.execute(
            """SELECT users.id, users.email, users.name FROM sessions
               JOIN users ON users.id = sessions.user_id
               WHERE sessions.token_hash = ? AND sessions.expires_at > datetime('now')
                 AND users.revoked_at IS NULL""",
            (_hash(token),),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    display = row["name"] or row["email"].split("@")[0]
    initials = "".join(part[0] for part in display.split()[:2]).upper() or display[:2].upper()
    return {"id": row["id"], "email": row["email"], "display_name": display, "initials": initials}


def end_session(db_path, token: str | None) -> None:
    if not token:
        return
    conn = db.connect(db_path)
    try:
        with conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))
    finally:
        conn.close()


def invite(db_path, email: str) -> str:
    email = email.strip()
    if "@" not in email:
        raise ValueError("%r is not an email address" % email)
    conn = db.connect(db_path)
    try:
        with conn:
            existing = conn.execute("SELECT id, revoked_at FROM users WHERE email = ?", (email,)).fetchone()
            if existing and existing["revoked_at"] is None:
                return "%s is already invited" % email
            if existing:
                conn.execute("UPDATE users SET revoked_at = NULL, invited_at = datetime('now') WHERE id = ?", (existing["id"],))
                return "%s is invited again" % email
            conn.execute("INSERT INTO users (email) VALUES (?)", (email,))
            return "%s is invited" % email
    finally:
        conn.close()


def revoke(db_path, email: str) -> str:
    conn = db.connect(db_path)
    try:
        with conn:
            user = conn.execute("SELECT id FROM users WHERE email = ?", (email.strip(),)).fetchone()
            if not user:
                return "%s was never invited" % email
            conn.execute("UPDATE users SET revoked_at = datetime('now') WHERE id = ?", (user["id"],))
            conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
            return "%s can no longer sign in" % email
    finally:
        conn.close()
