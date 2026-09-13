"""Dashboard checks through FastAPI's test client. Needs the [web] extra; no network.

    python tests/test_web.py
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from shorts.web import db  # noqa: E402
from shorts.web.app import create_app  # noqa: E402


def test_health_and_the_empty_runs_page():
    with tempfile.TemporaryDirectory() as tmp:
        client = TestClient(create_app(Path(tmp)))
        assert client.get("/health").json() == {"ok": True}

        home = client.get("/", follow_redirects=False)
        assert home.status_code == 303 and home.headers["location"] == "/runs"

        page = client.get("/runs")
        assert page.status_code == 200
        assert 'aria-current="page"' in page.text and "No runs yet" in page.text
        assert client.get("/static/app.css").status_code == 200


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
