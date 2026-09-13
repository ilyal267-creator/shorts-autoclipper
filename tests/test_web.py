"""Dashboard checks through FastAPI's test client. Needs the [web] extra; no network.

    python tests/test_web.py
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from shorts.web import auth, db  # noqa: E402
from shorts.web.app import create_app  # noqa: E402

CLIENT = {"GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com", "GOOGLE_CLIENT_SECRET": "test-secret"}


def id_token(email: str, name: str = "Test", **overrides) -> str:
    claims = {
        "iss": "https://accounts.google.com", "aud": CLIENT["GOOGLE_CLIENT_ID"], "exp": time.time() + 600,
        "email": email, "email_verified": True, "given_name": name, **overrides,
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode()
    return "header.%s.signature" % payload


def sign_in(client: TestClient, email: str, **overrides):
    """Run the Google round trip with the token endpoint stubbed; returns the callback response."""
    start = client.get("/auth/google", follow_redirects=False)
    assert start.status_code == 303, start.text
    state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers["location"]).query)["state"][0]
    with mock.patch.object(auth.pipeline_auth, "_post_form", lambda url, fields: {"id_token": id_token(email, **overrides)}):
        return client.get("/auth/google/callback", params={"code": "c0de", "state": state}, follow_redirects=False)


def app_client(tmp: str) -> tuple[TestClient, Path]:
    app = create_app(Path(tmp))
    return TestClient(app), app.state.db_path


def test_health_and_the_empty_runs_page():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = app_client(tmp)
        assert client.get("/health").json() == {"ok": True}
        assert client.get("/static/app.css").status_code == 200

        auth.invite(db_path, "ilya@example.com")
        assert sign_in(client, "ilya@example.com", name="Ilya").headers["location"] == "/runs"
        page = client.get("/runs")
        assert page.status_code == 200
        assert 'aria-current="page"' in page.text and "No runs yet" in page.text and "Ilya" in page.text


def test_only_invited_emails_get_a_session():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = app_client(tmp)
        for path in ("/", "/runs"):
            assert client.get(path, follow_redirects=False).headers["location"] == "/signin"

        stranger = sign_in(client, "stranger@example.com")
        assert stranger.headers["location"] == "/signin?error=not_invited"
        assert "session" not in client.cookies
        assert "has no invite" in client.get("/signin?error=not_invited").text

        # unverified email and a token minted for another app are both refused, even if invited
        auth.invite(db_path, "tester@example.com")
        assert sign_in(client, "tester@example.com", email_verified=False).headers["location"] == "/signin?error=failed"
        assert sign_in(client, "tester@example.com", aud="someone-else").headers["location"] == "/signin?error=failed"
        assert "session" not in client.cookies

        assert sign_in(client, "TESTER@example.com").headers["location"] == "/runs"  # email case doesn't matter
        assert client.get("/runs", follow_redirects=False).status_code == 200

        client.post("/signout", follow_redirects=False)
        assert client.get("/runs", follow_redirects=False).headers["location"] == "/signin"


def test_revoking_ends_sessions_and_a_forged_callback_is_refused():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = app_client(tmp)
        auth.invite(db_path, "tester@example.com")
        sign_in(client, "tester@example.com")
        assert client.get("/runs", follow_redirects=False).status_code == 200

        assert "can no longer sign in" in auth.revoke(db_path, "tester@example.com")
        assert client.get("/runs", follow_redirects=False).headers["location"] == "/signin"
        assert sign_in(client, "tester@example.com").headers["location"] == "/signin?error=not_invited"

        # a callback whose state this browser never started is rejected before any code exchange
        auth.invite(db_path, "tester@example.com")
        fresh = TestClient(client.app)
        called = []
        with mock.patch.object(auth.pipeline_auth, "_post_form", lambda *a: called.append(a) or {}):
            forged = fresh.get("/auth/google/callback", params={"code": "c0de", "state": "attacker"}, follow_redirects=False)
        assert forged.headers["location"] == "/signin?error=failed" and called == []
        assert "session" not in fresh.cookies

        # the database never holds a usable session token
        token = client.cookies.get("session") or ""
        conn = db.connect(db_path)
        assert all(token not in row[0] for row in conn.execute("SELECT token_hash FROM sessions"))
        conn.close()


def test_migrations_run_once_and_survive_a_restart():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested" / "shorts.db"
        assert db.migrate(path) == len(db.MIGRATIONS)
        conn = db.connect(path)
        conn.execute("INSERT INTO users (email) VALUES ('Tester@Example.com')")
        conn.commit()
        conn.close()

        assert db.migrate(path) == len(db.MIGRATIONS)  # a restart re-runs nothing
        conn = db.connect(path)
        try:
            assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 1
            try:
                conn.execute("INSERT INTO users (email) VALUES ('tester@example.com')")
                raise AssertionError("an email invited twice in different case must be refused")
            except sqlite3.IntegrityError:
                pass
        finally:
            conn.close()


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok  %s" % test.__name__)
    print("\n%d checks passed" % len(tests))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
