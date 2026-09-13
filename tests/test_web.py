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


def signed_in(tmp: str, email: str = "tester@example.com") -> tuple[TestClient, Path]:
    client, db_path = app_client(tmp)
    auth.invite(db_path, email)
    assert sign_in(client, email).headers["location"] == "/runs"
    return client, db_path


def make_video(path: Path, seconds: int = 4, audio_tracks: int = 2) -> None:
    import subprocess

    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
           "-f", "lavfi", "-i", "testsrc=size=640x360:rate=25:duration=%d" % seconds]
    for i in range(audio_tracks):
        cmd += ["-f", "lavfi", "-i", "sine=frequency=%d:duration=%d" % (300 + 200 * i, seconds)]
    cmd += ["-map", "0:v"] + sum((["-map", "%d:a" % (i + 1)] for i in range(audio_tracks)), [])
    cmd += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)]
    subprocess.run(cmd, check=True)


def upload(client: TestClient, path: Path, name: str = "talk.mp4"):
    return client.post(
        "/api/uploads", content=path.read_bytes(),
        headers={"X-Filename": urllib.parse.quote(name), "Content-Type": "application/octet-stream"},
    )


def start_form(**changes) -> dict:
    form = {"clip_count": "2", "framing": "fit", "platforms": ["tiktok", "youtube_shorts"],
            "audio_track": "0", "rights": "yes"}
    form.update(changes)
    return {k: v for k, v in form.items() if v is not None}


def test_a_new_run_is_uploaded_checked_and_queued_with_its_settings():
    import shutil

    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH")
        return
    from shorts import config as config_mod

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = signed_in(tmp)
        video = Path(tmp) / "in.mp4"
        make_video(video, audio_tracks=2)

        sent = upload(client, video, name="Ep 12 (final).mp4")
        assert sent.status_code == 200, sent.text
        run_id = sent.json()["run_id"]
        setup = client.get(sent.json()["next"])
        assert setup.status_code == 200
        assert "Ep 12 (final).mp4" in setup.text and "640×360" in setup.text
        assert "This file has 2 audio tracks" in setup.text and "Track 2 · aac" in setup.text

        # the rights box is enforced by the server, not only by the browser
        refused = client.post("/runs/%s/start" % run_id, data=start_form(rights=None), follow_redirects=False)
        assert refused.headers["location"].endswith("error=rights")
        assert "Confirm you have the rights" in client.get(refused.headers["location"]).text
        bad_track = client.post("/runs/%s/start" % run_id, data=start_form(audio_track="7"), follow_redirects=False)
        assert bad_track.headers["location"].endswith("error=track")

        started = client.post("/runs/%s/start" % run_id, data=start_form(audio_track="1"), follow_redirects=False)
        assert started.status_code == 303 and "error" not in started.headers["location"]
        conn = db.connect(db_path)
        row = conn.execute("SELECT status, config_json FROM runs WHERE id = ?", (run_id,)).fetchone()
        conn.close()
        assert row["status"] == "queued"
        raw = json.loads(row["config_json"])
        assert raw["audio_track"] == 1 and raw["reframe_mode"] == "fit" and raw["clip_count"] == 2
        cfg = config_mod.from_dict(raw)
        assert [a["platform"] for a in cfg.connected_accounts] == ["tiktok", "youtube_shorts"]
        assert cfg.posting_mode == "draft_for_approval" and cfg.rights_confirmed
        assert all("rights" not in flag and "posting_mode" not in flag for flag in cfg.flags), cfg.flags

        # a started run can't be started again, and another tester can't see it at all
        again = client.post("/runs/%s/start" % run_id, data=start_form(), follow_redirects=False)
        assert again.status_code == 409
        other, _ = app_client(tmp)
        auth.invite(db_path, "other@example.com")
        sign_in(other, "other@example.com")
        assert other.get("/runs/%s/setup" % run_id).status_code == 404


def test_uploads_over_the_limits_or_unreadable_are_refused_and_leave_nothing():
    import shutil

    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH")
        return
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = signed_in(tmp)
        runs_dir = Path(tmp) / "runs"
        video = Path(tmp) / "in.mp4"
        make_video(video, seconds=4, audio_tracks=1)

        junk = Path(tmp) / "notes.mp4"
        junk.write_bytes(b"this is not a video" * 100)
        unreadable = upload(client, junk)
        assert unreadable.status_code == 422 and "couldn't be read" in unreadable.json()["error"]

        with mock.patch.dict(os.environ, {"SHORTS_MAX_UPLOAD_BYTES": "1000"}):
            big = upload(client, video)
        assert big.status_code == 413

        with mock.patch.dict(os.environ, {"SHORTS_MAX_SOURCE_MINUTES": "0.02"}):  # 1.2 seconds
            long = upload(client, video)
        assert long.status_code == 422 and "minutes" in long.json()["error"]

        assert not runs_dir.exists() or not any(runs_dir.iterdir())
        conn = db.connect(db_path)
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 0
        conn.close()

        # the daily cap counts started runs, and is enforced when starting
        with mock.patch.dict(os.environ, {"SHORTS_RUNS_PER_DAY": "1"}):
            first = upload(client, video).json()["run_id"]
            second = upload(client, video).json()["run_id"]
            ok = client.post("/runs/%s/start" % first, data=start_form(audio_track="0"), follow_redirects=False)
            assert "error" not in ok.headers["location"]
            capped = client.post("/runs/%s/start" % second, data=start_form(audio_track="0"), follow_redirects=False)
            assert capped.headers["location"].endswith("error=cap")
            assert "used today's runs" in client.get("/runs/new").text

        stranger = TestClient(client.app)
        assert stranger.post("/api/uploads", content=b"x").status_code == 401


def queue_run(db_path: Path, work: Path, user_email: str = "tester@example.com", narrated: bool = False) -> str:
    """A queued run straight into the database, with a transcript so no whisper is needed."""
    from test_render_e2e import make_source, make_transcript  # same folder as this file

    source, transcript = work / "source.mp4", work / "transcript.json"
    if not source.exists():
        make_source(source)
        make_transcript(transcript)
    conn = db.connect(db_path)
    user = conn.execute("SELECT id FROM users WHERE email = ?", (user_email,)).fetchone()
    run_id = "run%d" % (conn.execute("SELECT count(*) FROM runs").fetchone()[0] + 1)
    raw = {
        "source_video": str(source), "source_transcript": str(transcript),
        "connected_accounts": [{"platform": "tiktok", "account_id": "pending", "handle": ""},
                               {"platform": "youtube_shorts", "account_id": "pending", "handle": ""}],
        "clip_count": 2, "clip_length_range": [10, 30], "posting_mode": "draft_for_approval",
        "rights_confirmed": True, "voiceover": {"enabled": narrated}, "output_dir": str(work / run_id / "out"),
    }
    with conn:
        conn.execute(
            """INSERT INTO runs (id, user_id, status, source_name, source_path, source_bytes, probe_json, config_json, queued_at)
               VALUES (?, ?, 'queued', 'talk.mp4', ?, 1, '{"audio_codecs": ["aac"]}', ?, datetime('now'))""",
            (run_id, user["id"], str(source), json.dumps(raw)),
        )
    conn.close()
    return run_id


def test_the_worker_runs_a_queued_run_and_the_progress_screen_follows_it():
    import shutil

    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH")
        return
    from shorts.web import worker

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**CLIENT, "SHORTS_PROVIDER": "mock"}):
        client, db_path = signed_in(tmp)
        run_id = queue_run(db_path, Path(tmp))

        waiting = client.get("/api/runs/%s/progress" % run_id).json()
        assert waiting["status"] == "queued" and not waiting["terminal"]
        assert all(s["state"] == "pending" for s in waiting["stages"])
        assert "voiceover" not in [s["key"] for s in waiting["stages"]]  # not narrated, so not shown

        worker.work(db_path, once=True)

        done = client.get("/api/runs/%s/progress" % run_id).json()
        assert done["status"] == "needs_review" and done["terminal"], done
        assert all(s["state"] == "done" for s in done["stages"]), done["stages"]
        assert [c["clip_id"] for c in done["clips"]] == ["clip_1", "clip_2"]
        assert any("clip_2: delivering" in line for line in done["log"])
        assert any(line.endswith("-> clip_1.mp4") for line in done["log"]), done["log"]
        assert not any(str(Path(tmp)) in line or tmp.replace("\\", "/") in line for line in done["log"])
        page = client.get("/runs/%s" % run_id)
        assert page.status_code == 200 and "Drafts ready" in page.text and "Cancel run" not in page.text

        conn = db.connect(db_path)
        row = conn.execute("SELECT summary_json, started_at, finished_at FROM runs WHERE id = ?", (run_id,)).fetchone()
        stages = [r[0] for r in conn.execute("SELECT stage FROM run_events WHERE run_id = ? AND stage IS NOT NULL ORDER BY id", (run_id,))]
        conn.close()
        assert json.loads(row["summary_json"])["clips"][0]["platforms"]["tiktok"]["status"] == "draft"
        assert row["started_at"] and row["finished_at"]
        assert stages[:3] == ["probe", "transcribe", "plan"] and stages[-1] == "deliver"

        other, _ = app_client(tmp)
        auth.invite(db_path, "other@example.com")
        sign_in(other, "other@example.com")
        assert other.get("/api/runs/%s/progress" % run_id).status_code == 404
        assert other.post("/runs/%s/cancel" % run_id, follow_redirects=False).status_code == 404


def test_cancel_and_a_dead_worker_both_end_a_run_with_the_reason():
    import shutil

    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH")
        return
    from shorts.web import worker

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**CLIENT, "SHORTS_PROVIDER": "mock"}):
        client, db_path = signed_in(tmp)

        # cancelled while still queued: it never starts
        queued = queue_run(db_path, Path(tmp))
        client.post("/runs/%s/cancel" % queued, follow_redirects=False)
        assert worker.claim_next(db_path) is None
        assert client.get("/api/runs/%s/progress" % queued).json()["status"] == "cancelled"

        # cancelled while running: the worker stops at the next stage it enters
        running = queue_run(db_path, Path(tmp))
        claimed = worker.claim_next(db_path)
        assert claimed["id"] == running
        client.post("/runs/%s/cancel" % running, follow_redirects=False)
        assert client.get("/api/runs/%s/progress" % running).json()["cancelling"]
        assert worker.execute(db_path, claimed) == "cancelled"
        conn = db.connect(db_path)
        events = conn.execute("SELECT count(*) FROM run_events WHERE run_id = ?", (running,)).fetchone()[0]
        conn.close()
        assert events == 1, events  # stopped at the very first stage, nothing rendered

        # a worker that died mid-run: the next worker start says what happened
        crashed = queue_run(db_path, Path(tmp))
        worker.claim_next(db_path)
        worker.work(db_path, once=True)
        p = client.get("/api/runs/%s/progress" % crashed).json()
        assert p["status"] == "failed" and "restarted" in p["error"]
        assert "restarted" in client.get("/runs/%s" % crashed).text


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
