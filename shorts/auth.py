"""YouTube OAuth for an installed app: one browser sign-in, then refresh tokens forever after.

`python -m shorts auth youtube` runs the loopback flow once and stores the refresh token under
.secrets/. Publishing then trades it for a short-lived access token on each run, so nothing
unattended ever needs a person — or a pasted token — again.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path

YOUTUBE_UPLOAD_SCOPE = "https://www.googleapis.com/auth/youtube.upload"
SECRETS_DIR = Path(os.getenv("SHORTS_SECRETS_DIR", ".secrets"))


class AuthError(Exception):
    pass


def client_path() -> Path:
    return Path(os.getenv("YOUTUBE_CLIENT_SECRETS", SECRETS_DIR / "youtube_client.json"))


def token_path() -> Path:
    return Path(os.getenv("YOUTUBE_TOKEN_FILE", SECRETS_DIR / "youtube_token.json"))


def load_client() -> dict:
    path = client_path()
    if not path.exists():
        raise AuthError(
            "no OAuth client at %s — download a Desktop-app client JSON from Google Cloud "
            "Credentials and save it there" % path
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    client = data.get("installed")
    if not client:
        raise AuthError("%s is not a Desktop-app client (no 'installed' section)" % path)
    return client


def pkce_pair() -> tuple[str, str]:
    """RFC 7636: the verifier stays here, only its S256 hash goes to the browser."""
    verifier = secrets.token_urlsafe(64)[:96]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def consent_url(client: dict, redirect_uri: str, challenge: str, state: str) -> str:
    return client["auth_uri"] + "?" + urllib.parse.urlencode(
        {
            "client_id": client["client_id"],
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": YOUTUBE_UPLOAD_SCOPE,
            "access_type": "offline",  # ask for a refresh token
            # Always show the account chooser (the browser may already hold another Google
            # account) and always re-consent, so a refresh token comes back every time.
            "prompt": "select_account consent",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
        }
    )


def _post_form(url: str, fields: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(fields).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        raise AuthError("token endpoint said %d: %s" % (exc.code, body[:300])) from exc


def authorize_youtube(timeout: float = 300.0) -> Path:
    """Open the browser, wait for the person to approve, keep the refresh token."""
    client = load_client()
    verifier, challenge = pkce_pair()
    state = secrets.token_urlsafe(24)
    result: dict = {}

    class Catch(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server's naming
            query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result.update({key: values[0] for key, values in query.items()})
            ok = "code" in result and result.get("state") == state
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            message = "Signed in. You can close this tab." if ok else "Sign-in did not complete."
            self.wfile.write(("<p style='font:16px sans-serif'>%s</p>" % message).encode("utf-8"))

        def log_message(self, *args):  # keep the auth code out of the console
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Catch)
    server.timeout = 1.0
    redirect_uri = "http://127.0.0.1:%d/" % server.server_address[1]
    url = consent_url(client, redirect_uri, challenge, state)

    print("Opening your browser to sign in to Google. If it does not open, visit:\n\n%s\n" % url)
    webbrowser.open(url)

    deadline = time.monotonic() + timeout
    try:
        while not result and time.monotonic() < deadline:
            server.handle_request()
    finally:
        server.server_close()

    if not result:
        raise AuthError("no response from the browser within %ds" % timeout)
    if result.get("state") != state:
        raise AuthError("state mismatch — ignoring a response this sign-in did not start")
    if "error" in result:
        raise AuthError("Google refused the sign-in: %s" % result["error"])

    tokens = _post_form(
        client["token_uri"],
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "code": result["code"],
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
    )
    if "refresh_token" not in tokens:
        raise AuthError("Google returned no refresh token — revoke the app's access and retry")

    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"refresh_token": tokens["refresh_token"], "scope": tokens.get("scope", "")}),
        encoding="utf-8",
    )
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows: the .secrets/ gitignore and the user profile are the protection
    return path


_cached: dict = {}


def youtube_access_token() -> str:
    """A live access token: from the environment if given, else minted from the refresh token."""
    explicit = os.getenv("YOUTUBE_ACCESS_TOKEN")
    if explicit:
        return explicit
    if _cached.get("expires", 0) > time.time() + 60:
        return _cached["token"]

    path = token_path()
    if not path.exists():
        raise AuthError("not signed in to YouTube — run: python -m shorts auth youtube")
    refresh = json.loads(path.read_text(encoding="utf-8"))["refresh_token"]
    client = load_client()
    tokens = _post_form(
        client["token_uri"],
        {
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": refresh,
            "grant_type": "refresh_token",
        },
    )
    _cached.update(token=tokens["access_token"], expires=time.time() + int(tokens.get("expires_in", 3600)))
    return _cached["token"]
