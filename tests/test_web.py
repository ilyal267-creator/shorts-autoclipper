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
        assert page.status_code == 200 and "0 of 4 approved" in page.text and "Cancel run" not in page.text  # it's the review now

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


def test_the_runs_list_shows_only_your_own_runs_in_every_state():
    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = signed_in(tmp)
        other, _ = app_client(tmp)
        auth.invite(db_path, "other@example.com")
        sign_in(other, "other@example.com")

        conn = db.connect(db_path)
        me = conn.execute("SELECT id FROM users WHERE email = 'tester@example.com'").fetchone()[0]
        them = conn.execute("SELECT id FROM users WHERE email = 'other@example.com'").fetchone()[0]
        probe = json.dumps({"duration": 2527, "width": 3840, "height": 2160, "hdr": True, "audio_codecs": ["aac", "aac"]})
        config = json.dumps({"connected_accounts": [{"platform": "tiktok"}, {"platform": "youtube_shorts"}], "voiceover": {}})
        summary = json.dumps({"clips": [{}, {}, {}]})
        rows = [
            ("r_review", me, "needs_review", "Review me.mp4", summary, None),
            ("r_running", me, "running", "Running.mp4", None, "copy"),
            ("r_none", me, "no_clips", "Silent.mp4", json.dumps({"clips": []}), None),
            ("r_failed", me, "failed", "Broken.mp4", None, "transcribe"),
            ("r_cancel", me, "cancelled", "Stopped.mp4", None, None),
            ("r_setup", me, "uploaded", "Unfinished.mp4", None, None),
            ("r_theirs", them, "needs_review", "Someone else's secret.mp4", summary, None),
        ]
        with conn:
            for run_id, user_id, status, name, summ, stage in rows:
                conn.execute(
                    """INSERT INTO runs (id, user_id, status, source_name, source_path, source_bytes, probe_json, config_json, summary_json, stage, queued_at)
                       VALUES (?, ?, ?, ?, '/x/source.mp4', 1, ?, ?, ?, ?, CASE WHEN ? = 'uploaded' THEN NULL ELSE datetime('now') END)""",
                    (run_id, user_id, status, name, probe, None if status == "uploaded" else config, summ, stage, status),
                )
        conn.close()

        page = client.get("/runs").text
        for label in ("Needs review", "Write hooks, captions and narration", "No clips", "Failed", "Cancelled", "Not started"):
            assert label in page, label
        assert "Someone else" not in page and "r_theirs" not in page
        assert "42:07 · 3840×2160 · HDR · 2 audio tracks" in page
        assert 'href="/runs/r_setup/setup"' in page and 'href="/runs/r_review"' in page
        assert 'http-equiv="refresh"' in page  # a run is going, so the list keeps itself current

        # guessing someone else's run id gets the same answer as a run that doesn't exist
        for path in ("/runs/r_theirs", "/api/runs/r_theirs/progress", "/runs/r_theirs/setup", "/runs/nope"):
            assert client.get(path, follow_redirects=False).status_code == 404, path
        assert "Someone else" in other.get("/runs").text


def test_review_plays_each_version_and_keeps_approvals_and_edits():
    import shutil

    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH")
        return
    from shorts.web import worker

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**CLIENT, "SHORTS_PROVIDER": "mock"}):
        client, db_path = signed_in(tmp)
        run_id = queue_run(db_path, Path(tmp))
        worker.work(db_path, once=True)

        page = client.get("/runs/%s" % run_id).text
        assert "0 of 4 approved" in page and "Clip 1" in page and "Clip 2" in page
        assert page.count('class="card ') == 2 and "TikTok" in page and "YouTube Shorts" in page
        assert "MOCK provider" in page  # run-wide notes are shown before anything is approved
        assert 'src="/runs/%s/media/clip_1.mp4#t=0.1"' % run_id in page

        # the video plays and seeks (range requests), only for its owner, only files the run made
        video = client.get("/runs/%s/media/clip_1.mp4" % run_id, headers={"Range": "bytes=0-99"})
        assert video.status_code == 206 and len(video.content) == 100, video.status_code
        assert video.headers["content-type"] == "video/mp4"
        for name in ("clip_9.mp4", "..%2F..%2Fshorts.db", "summary.json"):
            assert client.get("/runs/%s/media/%s" % (run_id, name)).status_code == 404, name
        other, _ = app_client(tmp)
        auth.invite(db_path, "other@example.com")
        sign_in(other, "other@example.com")
        assert other.get("/runs/%s/media/clip_1.mp4" % run_id).status_code == 404
        assert other.post("/runs/%s/clips/clip_1/tiktok/approve" % run_id, follow_redirects=False).status_code == 404

        # approve, undo, approve again: it sticks across reloads
        base = "/runs/%s/clips/clip_1/tiktok" % run_id
        assert client.post(base + "/approve", follow_redirects=False).headers["location"].endswith("#tiktok")
        assert "1 of 4 approved" in client.get("/runs/%s" % run_id).text
        client.post(base + "/unapprove")
        assert "0 of 4 approved" in client.get("/runs/%s" % run_id).text
        client.post(base + "/approve")

        # an edit is saved, cleaned up, and sends the draft back for approval
        client.post(base + "/copy", data={"caption": "Cut scope, not corners.", "hashtags": "coding, #DevTok #coding"})
        edited = client.get("/runs/%s?clip=clip_1" % run_id).text
        assert "Cut scope, not corners." in edited and "#coding #DevTok" in edited and "0 of 4 approved" in edited
        conn = db.connect(db_path)
        row = conn.execute("SELECT state, caption, hashtags_json FROM clip_versions WHERE run_id = ? AND clip_id = 'clip_1' AND platform = 'tiktok'", (run_id,)).fetchone()
        conn.close()
        assert row["state"] == "draft" and json.loads(row["hashtags_json"]) == ["#coding", "#DevTok"]

        bad_tags = client.post(base + "/copy", data={"caption": "x", "hashtags": "#ok #not-ok!"}, follow_redirects=False)
        assert bad_tags.headers["location"].endswith("error=hashtags#tiktok")
        long_title = client.post("/runs/%s/clips/clip_1/youtube_shorts/copy" % run_id,
                                 data={"title": "t" * 101, "caption": "x", "hashtags": ""}, follow_redirects=False)
        assert "error=title" in long_title.headers["location"]
        assert "A YouTube title needs" in client.get(long_title.headers["location"]).text
        assert client.post("/runs/%s/clips/clip_7/tiktok/approve" % run_id, follow_redirects=False).status_code == 404

        client.post("/runs/%s/approve-all" % run_id)
        assert "All 4 approved" in client.get("/runs/%s" % run_id).text


def connect_youtube(client: TestClient, channel_id: str = "UCtester", handle: str = "Test channel"):
    """Walk the real connect flow with Google and the YouTube API stubbed out."""
    from shorts.web import connections

    start = client.get("/connect/youtube", follow_redirects=False)
    assert start.status_code == 303, start.text
    url = start.headers["location"]
    assert "youtube.upload" in url and "access_type=offline" in url and "prompt=select_account+consent" in url
    state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
    with mock.patch.object(connections.pipeline_auth, "_post_form",
                           lambda endpoint, fields: {"refresh_token": "refresh-" + channel_id, "access_token": "live-token"}), \
            mock.patch.object(connections, "channel_of", lambda token: {"account_id": channel_id, "handle": handle}):
        return client.get("/connect/youtube/callback", follow_redirects=False,
                          params={"code": "c0de", "state": state, "scope": "https://www.googleapis.com/auth/youtube.upload"})


def test_connecting_youtube_seals_the_token_and_shows_the_channel():
    from shorts.web import connections, crypto

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = signed_in(tmp)
        assert "Not connected" in client.get("/accounts").text

        assert connect_youtube(client).headers["location"] == "/accounts"
        page = client.get("/accounts").text
        assert "Test channel" in page and "Connected" in page and "Connect another channel" in page

        # the refresh token is unreadable in the database file itself
        conn = db.connect(db_path)
        row = conn.execute("SELECT * FROM connections").fetchone()
        conn.close()
        assert row["platform"] == "youtube_shorts" and row["account_id"] == "UCtester"
        assert b"refresh-UCtester" not in bytes(row["secret"])
        assert crypto.open_sealed(Path(tmp), row["secret"])["refresh_token"] == "refresh-UCtester"

        # a callback this browser never started, and one where the upload box was left unticked
        fresh = TestClient(client.app)
        auth.invite(db_path, "second@example.com")
        sign_in(fresh, "second@example.com")
        forged = fresh.get("/connect/youtube/callback", params={"code": "x", "state": "attacker"}, follow_redirects=False)
        assert forged.headers["location"] == "/accounts?error=failed"
        start = client.get("/connect/youtube", follow_redirects=False)
        state = urllib.parse.parse_qs(urllib.parse.urlparse(start.headers["location"]).query)["state"][0]
        refused = client.get("/connect/youtube/callback", follow_redirects=False,
                             params={"code": "c0de", "state": state, "scope": "openid email"})
        assert refused.headers["location"] == "/accounts?error=refused"
        assert "give us permission to upload" in client.get("/accounts?error=refused").text

        # disconnecting is mine to do, and only mine
        conn = db.connect(db_path)
        connection_id = conn.execute("SELECT id FROM connections").fetchone()[0]
        conn.close()
        assert fresh.post("/accounts/%d/disconnect" % connection_id, follow_redirects=False).status_code == 404
        client.post("/accounts/%d/disconnect" % connection_id)
        assert "Not connected" in client.get("/accounts").text
        assert connections.listed(db_path, 1) == []


def test_publishing_a_draft_uses_this_testers_own_channel():
    import shutil

    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH")
        return
    from shorts import publish as publishing
    from shorts.web import connections, worker

    sent = []

    class Response:
        def __init__(self, body=None, headers=None, status=200):
            self.status_code, self.text = status, "{}"
            self.headers, self._body = headers or {}, body or {}

        def json(self):
            return self._body

    class FakeRequests:
        def post(self, url, **kw):
            sent.append(kw)
            return Response(headers={"Location": "https://upload.example/s"})

        def put(self, url, **kw):
            return Response({"id": "yt_posted_1"})

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**CLIENT, "SHORTS_PROVIDER": "mock"}):
        client, db_path = signed_in(tmp)
        run_id = queue_run(db_path, Path(tmp))
        worker.work(db_path, once=True)
        base = "/runs/%s/clips/clip_1/youtube_shorts" % run_id

        # nothing connected yet: the card offers no publish button and the endpoint refuses
        assert "Publish privately" not in client.get("/runs/%s" % run_id).text
        client.post(base + "/approve")
        refused = client.post(base + "/publish", follow_redirects=False)
        assert refused.headers["location"].endswith("error=not_connected#youtube_shorts")

        connect_youtube(client, channel_id="UCmine", handle="My channel")
        assert "Publish privately" in client.get("/runs/%s" % run_id).text

        # an approved draft posts with this tester's token and the copy as it reads now
        client.post(base + "/copy", data={"title": "Scope, not typing", "caption": "edited", "hashtags": "#shorts"})
        client.post(base + "/approve")
        with mock.patch.object(connections.pipeline_auth, "_post_form", lambda endpoint, fields: {"access_token": "live-for-UCmine", "expires_in": 3600}), \
                mock.patch.object(publishing, "_requests", lambda: FakeRequests()), \
                mock.patch.object(publishing.time, "sleep", lambda s: None):
            posted = client.post(base + "/publish", follow_redirects=False)
        assert "error" not in posted.headers["location"], posted.headers["location"]
        assert sent and sent[0]["headers"]["Authorization"] == "Bearer live-for-UCmine"
        assert sent[0]["json"]["snippet"]["title"] == "Scope, not typing"
        assert sent[0]["json"]["status"]["privacyStatus"] == "private"

        page = client.get("/runs/%s" % run_id).text
        assert "Posted" in page and "https://youtu.be/yt_posted_1" in page and "1 of 4 posted" in page

        # a draft that is not approved never posts
        other = "/runs/%s/clips/clip_2/youtube_shorts" % run_id
        assert client.post(other + "/publish", follow_redirects=False).headers["location"].endswith("error=not_approved#youtube_shorts")

        # a revoked connection asks to be reconnected instead of failing quietly
        conn = db.connect(db_path)
        connection_id = conn.execute("SELECT id FROM connections").fetchone()[0]
        conn.close()
        connections._tokens.clear()
        client.post(other + "/approve")
        with mock.patch.object(connections.pipeline_auth, "_post_form",
                               mock.Mock(side_effect=connections.pipeline_auth.AuthError("token endpoint said 400"))):
            gone = client.post(other + "/publish", follow_redirects=False)
        assert gone.headers["location"].endswith("error=not_connected#youtube_shorts")
        conn = db.connect(db_path)
        assert conn.execute("SELECT status FROM connections WHERE id = ?", (connection_id,)).fetchone()[0] == "reconnect"
        conn.close()
        assert "stopped working" in client.get("/accounts").text


def test_a_channel_may_be_clipped_only_on_ownership_or_permission():
    from shorts import sources
    from shorts.web import channels

    def resolving(mapping):
        def resolve(url):
            key = url.strip().lower()
            if key not in mapping:
                raise sources.SourceError("not a channel")
            channel_id, title = mapping[key]
            return sources.Channel(channel_id, title)
        return resolve

    known = {
        "@mine": ("UCmine", "My channel"),
        "@partner": ("UCpartner", "Partner stream"),
        "@stranger": ("UCstranger", "Someone else"),
        "https://www.youtube.com/@second": ("UCsecond", "Second partner"),
    }

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = signed_in(tmp)
        connect_youtube(client, channel_id="UCmine", handle="My channel")

        # no permission ticked: everything that is not yours lands with no rights on file
        with mock.patch.object(channels.sources, "resolve", resolving(known)):
            client.post("/channels", data={"urls": "@mine\n@stranger\nnot a channel at all"})
        page = client.get("/channels").text
        assert "Your own channel" in page and "No rights on file" in page
        assert "Clips from here stop in Review" in page

        rows = {row["channel_id"]: row for row in channels.listed(db_path, 1)}
        assert set(rows) == {"UCmine", "UCstranger"}, list(rows)
        assert rows["UCmine"]["rights"] == "owned" and rows["UCstranger"]["rights"] == "none"

        # ownership is not something the form can claim: it is checked against the connection
        with mock.patch.object(channels.sources, "resolve", resolving(known)):
            client.post("/channels", data={"urls": "@stranger", "has_permission": "no",
                                           "granted_by": "me", "granted_at": "2026-10-18", "evidence": "trust me"})
        assert channels.listed(db_path, 1)[0]["rights"] in ("none", "owned")
        assert {r["channel_id"]: r["rights"] for r in channels.listed(db_path, 1)}["UCstranger"] == "none"

        # permission needs a who, a when and a where - a bare tick is refused
        with mock.patch.object(channels.sources, "resolve", resolving(known)):
            thin = client.post("/channels", data={"urls": "@partner", "has_permission": "yes",
                                                  "granted_by": "", "granted_at": "", "evidence": ""},
                               follow_redirects=False)
        assert thin.headers["location"] == "/channels?error=permission"
        assert "needs a name, a date" in client.get("/channels?error=permission").text
        assert "UCpartner" not in {r["channel_id"] for r in channels.listed(db_path, 1)}

        # with the record filled in, two partners can be added at once under one permission
        with mock.patch.object(channels.sources, "resolve", resolving(known)):
            client.post("/channels", data={
                "urls": "@partner\nhttps://www.youtube.com/@second", "has_permission": "yes",
                "granted_by": "Dana Reed, channel manager", "granted_at": "2026-10-17",
                "evidence": "email: Re: clipping programme", "rules": "no sponsor segments",
                "attribution": "Clipped from @partner with permission",
            })
        rows = {row["channel_id"]: row for row in channels.listed(db_path, 1)}
        assert rows["UCpartner"]["rights"] == "permitted" and rows["UCsecond"]["rights"] == "permitted"
        assert rows["UCpartner"]["granted_by"].startswith("Dana Reed")
        assert "no sponsor segments" in client.get("/channels").text

        # the gate: only ownership or permission may post by itself, and the server says so
        stranger_id, partner_id = rows["UCstranger"]["id"], rows["UCpartner"]["id"]
        blocked = client.post("/channels/%d/auto" % stranger_id, data={"auto_publish": "yes"}, follow_redirects=False)
        assert blocked.headers["location"] == "/channels?error=auto_blocked"
        assert channels.listed(db_path, 1) and not any(r["auto_publish"] for r in channels.listed(db_path, 1))
        client.post("/channels/%d/auto" % partner_id, data={"auto_publish": "yes"})
        assert {r["channel_id"]: r["auto_publish"] for r in channels.listed(db_path, 1)}["UCpartner"] == 1

        # pausing, removing, and another tester's channels are not mine to touch
        client.post("/channels/%d/pause" % partner_id)
        assert "Resume" in client.get("/channels").text
        other, _ = app_client(tmp)
        auth.invite(db_path, "other@example.com")
        sign_in(other, "other@example.com")
        assert other.get("/channels").text.count("account-card") == 0
        for path in ("auto", "pause", "remove"):
            assert other.post("/channels/%d/%s" % (partner_id, path), data={"auto_publish": "yes"},
                              follow_redirects=False).status_code == 404
        client.post("/channels/%d/remove" % stranger_id)
        assert "UCstranger" not in {r["channel_id"] for r in channels.listed(db_path, 1)}


def test_the_rights_record_travels_with_the_run():
    from shorts import compliance, config as config_mod
    from shorts.web import channels

    base = {
        "source_video": "x.mp4",
        "connected_accounts": [{"platform": "tiktok", "account_id": "a", "handle": "@x"}],
        "rights_confirmed": True,
    }
    permitted = {"basis": "permitted", "channel": "Partner stream", "granted_by": "Dana Reed",
                 "granted_at": "2026-10-17", "evidence": "email: Re: clipping programme"}

    cfg = config_mod.from_dict({**base, "rights": permitted})
    assert cfg.rights["granted_by"] == "Dana Reed"
    verdict = compliance.check(cfg, "words", {}, {"verdict": "pass"})
    assert verdict.ok and any("with permission from Dana Reed" in flag for flag in verdict.flags)

    bare = config_mod.from_dict({**base, "rights": {"basis": "none", "channel": "Someone else"}})
    verdict = compliance.check(bare, "words", {}, {"verdict": "pass"})
    assert verdict.ok, verdict.reasons  # still clippable into drafts
    assert any("must not be posted automatically" in flag for flag in verdict.flags)

    # an upload the tester made themselves carries no channel record and is not nagged about one
    own = config_mod.from_dict(base)
    assert not any("rights record" in flag for flag in compliance.check(own, "words", {}, {"verdict": "pass"}).flags)

    # and what the page shows is what the run would carry
    row = {
        "rights": "permitted", "title": "Partner stream", "channel_id": "UCpartner",
        "granted_by": "Dana Reed", "granted_at": "2026-10-17", "evidence": "email",
        "attribution": "Clipped with permission", "auto_publish": 1,
    }
    record = channels.rights_of(row)
    assert record["may_auto_publish"] is True
    assert channels.rights_of({**row, "rights": "none"})["may_auto_publish"] is False


def test_a_partners_credit_line_is_added_to_the_caption():
    from shorts.web import channels, review

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = signed_in(tmp)
        conn = db.connect(db_path)
        with conn:
            conn.execute(
                """INSERT INTO channel_sources (id, user_id, channel_id, url, title, rights, attribution)
                   VALUES (7, 1, 'UCpartner', 'u', 'Partner stream', 'permitted', 'Clipped from @partner with permission')"""
            )
        with_source = conn.execute("SELECT 7 AS source_id").fetchone()
        conn.close()

        said = review.with_credit(db_path, with_source, 'Cut scope, not corners.')
        assert said.endswith('Clipped from @partner with permission')
        # never twice, and never on a clip that did not come from a partner channel
        assert review.with_credit(db_path, with_source, said) == said
        assert review.with_credit(db_path, {'source_id': None}, 'as is') == 'as is'


def add_source(db_path, channel_id="UCpartner", rights="permitted", **fields):
    conn = db.connect(db_path)
    row = {"user_id": 1, "channel_id": channel_id, "url": "https://www.youtube.com/channel/" + channel_id,
           "title": "Partner stream", "rights": rights, "active": 1, "last_video_id": None,
           "last_checked_at": None, "attribution": "Clipped from @partner with permission"}
    row.update(fields)
    with conn:
        cursor = conn.execute(
            """INSERT INTO channel_sources (user_id, channel_id, url, title, rights, active,
               last_video_id, last_checked_at, attribution)
               VALUES (:user_id, :channel_id, :url, :title, :rights, :active, :last_video_id,
                       :last_checked_at, :attribution)""",
            row,
        )
    conn.close()
    return cursor.lastrowid


def test_the_worker_checks_watched_channels_and_queues_what_is_new():
    import shutil

    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH")
        return
    from shorts import sources
    from shorts.web import channels, worker

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**CLIENT, "SHORTS_PROVIDER": "mock"}):
        client, db_path = signed_in(tmp)
        source_id = add_source(db_path)
        video = Path(tmp) / "upload.mp4"
        make_video(video, seconds=40, audio_tracks=1)  # long enough for a 15-45s clip

        uploads = [sources.Video("vid_new", "Best moment of the stream", "2026-10-18T09:00:00+00:00"),
                   sources.Video("vid_old", "Yesterday", "2026-10-17T09:00:00+00:00")]

        def fetch(url, into, max_minutes):
            into.mkdir(parents=True, exist_ok=True)
            saved = into / "source.mp4"
            saved.write_bytes(video.read_bytes())
            return saved

        with mock.patch.object(worker.sources, "feed", lambda channel_id: uploads), \
                mock.patch.object(worker.sources, "fetch", fetch):
            assert worker.check_channels(db_path, Path(tmp)) == 1

        conn = db.connect(db_path)
        run = conn.execute("SELECT * FROM runs WHERE source_id = ?", (source_id,)).fetchone()
        source = conn.execute("SELECT * FROM channel_sources WHERE id = ?", (source_id,)).fetchone()
        conn.close()
        assert run["status"] == "queued" and run["source_name"] == "Best moment of the stream"
        raw = json.loads(run["config_json"])
        assert raw["rights"]["basis"] == "permitted" and raw["rights"]["channel"] == "Partner stream"
        assert raw["posting_mode"] == "draft_for_approval"  # fetched uploads are drafts first
        assert source["last_video_id"] == "vid_new" and source["last_checked_at"]

        # checked again straight away: nothing new, nothing queued, no second copy of that video
        with mock.patch.object(worker.sources, "feed", lambda channel_id: uploads), \
                mock.patch.object(worker.sources, "fetch", fetch):
            assert worker.check_channels(db_path, Path(tmp), every_minutes=0) == 0
        conn = db.connect(db_path)
        assert conn.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
        conn.close()

        # a channel checked a minute ago is not due; one never checked is
        assert [s["id"] for s in channels.due(db_path, every_minutes=60)] == []
        assert [s["id"] for s in channels.due(db_path, every_minutes=0)] == [source_id]

        # the run the worker queued goes through the pipeline like any other
        worker.work(db_path, once=True)
        conn = db.connect(db_path)
        finished = conn.execute("SELECT status, summary_json FROM runs WHERE source_id = ?", (source_id,)).fetchone()
        conn.close()
        assert finished["status"] == "needs_review", finished["status"]
        summary = json.loads(finished["summary_json"])
        assert summary["rights"]["basis"] == "permitted"
        flags = summary["flags_for_human_review"]
        assert any("with permission" in flag for flag in flags), flags


def test_a_channel_in_trouble_pauses_itself_and_the_worker_carries_on():
    from shorts import sources
    from shorts.web import channels, worker

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, CLIENT):
        client, db_path = signed_in(tmp)
        refused = add_source(db_path, channel_id="UCrefused")
        ordinary = add_source(db_path, channel_id="UCordinary")

        def feed(channel_id):
            if channel_id == "UCrefused":
                raise sources.SourceError("YouTube refused this server. It wants cookies.", pause=True)
            raise sources.SourceError("that one is still live - it can be clipped once it ends")

        with mock.patch.object(worker.sources, "feed", feed):
            assert worker.check_channels(db_path, Path(tmp), every_minutes=0) == 0

        rows = {row["channel_id"]: row for row in channels.listed(db_path, 1)}
        assert rows["UCrefused"]["active"] == 0 and "cookies" in rows["UCrefused"]["paused_reason"]
        assert rows["UCordinary"]["active"] == 1 and "still live" in rows["UCordinary"]["paused_reason"]
        assert "Paused:" in client.get("/channels").text

        # over the daily cap, a channel is left alone rather than treated as broken
        with mock.patch.dict(os.environ, {"SHORTS_RUNS_PER_DAY": "0"}), \
                mock.patch.object(worker.sources, "feed", mock.Mock(side_effect=AssertionError("should not look"))):
            assert worker.check_channels(db_path, Path(tmp), every_minutes=0) == 0
        assert channels.listed(db_path, 1)[0]["active"] in (0, 1)


class FakeYouTube:
    """Stands in for the YouTube upload API, counting what was actually sent."""

    sent = []

    class Response:
        def __init__(self, body=None, headers=None):
            self.status_code, self.text = 200, "{}"
            self.headers, self._body = headers or {}, body or {}

        def json(self):
            return self._body

    def post(self, url, **kw):
        FakeYouTube.sent.append(kw)
        return self.Response(headers={"Location": "https://upload.example/s"})

    def put(self, url, **kw):
        return self.Response({"id": "posted_%d" % len(FakeYouTube.sent)})


def finished_channel_run(client, db_path, tmp, rights="permitted", auto=1, flag_clip_1=False):
    """A run that came from a watched channel and is sitting at needs_review."""
    from shorts.web import worker

    conn = db.connect(db_path)
    made = conn.execute("SELECT count(*) FROM channel_sources").fetchone()[0]
    conn.close()
    source_id = add_source(db_path, channel_id="UCpartner%d" % made, rights=rights)
    conn = db.connect(db_path)
    with conn:
        conn.execute("UPDATE channel_sources SET auto_publish = ? WHERE id = ?", (auto, source_id))
    conn.close()

    run_id = queue_run(db_path, Path(tmp))
    worker.work(db_path, once=True)

    conn = db.connect(db_path)
    summary = json.loads(conn.execute("SELECT summary_json FROM runs WHERE id = ?", (run_id,)).fetchone()[0])
    # the mock provider flags every clip with "no model-side policy screen ran", which would
    # hold back the whole run - correct in life, useless for showing the happy path here
    summary["flags_for_human_review"] = [
        flag for flag in summary["flags_for_human_review"] if "policy screen" not in flag
    ]
    if flag_clip_1:
        summary["flags_for_human_review"].append("clip_1: says something a person should look at")
    with conn:
        conn.execute("UPDATE runs SET source_id = ?, summary_json = ? WHERE id = ?",
                     (source_id, json.dumps(summary), run_id))
    conn.close()
    return run_id, source_id


def test_a_watched_channel_posts_by_itself_but_every_rail_can_stop_it():
    import shutil

    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH")
        return
    from shorts import publish as publishing
    from shorts.web import connections, posting, worker

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(
            os.environ, {**CLIENT, "SHORTS_PROVIDER": "mock", "SHORTS_POSTS_PER_ACCOUNT_PER_DAY": "2"}):
        client, db_path = signed_in(tmp)
        connect_youtube(client, channel_id="UCmine", handle="My channel")
        run_id, source_id = finished_channel_run(client, db_path, tmp, flag_clip_1=True)

        def run_auto(run_id):
            FakeYouTube.sent = []
            with mock.patch.object(connections.pipeline_auth, "_post_form",
                                   lambda endpoint, fields: {"access_token": "live", "expires_in": 3600}), \
                    mock.patch.object(publishing, "_requests", lambda: FakeYouTube()), \
                    mock.patch.object(publishing.time, "sleep", lambda s: None):
                return worker.post_what_we_may(db_path, Path(tmp), run_id)

        lines = run_auto(run_id)
        # clip_1 was flagged, so it waits for a person; clip_2 goes out by itself
        assert any("clip_1 youtube_shorts held back: compliance flagged it" in line for line in lines), lines
        assert any("clip_2 youtube_shorts posted" in line for line in lines), lines
        assert len(FakeYouTube.sent) == 1

        posts = posting.recent(db_path, 1)
        assert len(posts) == 1 and posts[0]["automatic"] is True
        assert posts[0]["channel"] == "Partner stream" and posts[0]["url"].startswith("https://youtu.be/")
        page = client.get("/posts").text
        assert "By itself" in page and "Partner stream" in page and "1 today" in page

        # the pause switch stops everything, by hand as well as by itself
        client.post("/posting/pause", data={"stop": "yes"})
        assert posting.paused(db_path, 1)
        second, _ = finished_channel_run(client, db_path, tmp)
        assert all("held back: posting is paused" in line for line in run_auto(second)), run_auto(second)
        assert FakeYouTube.sent == []
        by_hand = client.post("/runs/%s/clips/clip_1/youtube_shorts/approve" % second, follow_redirects=False)
        held = client.post("/runs/%s/clips/clip_1/youtube_shorts/publish" % second, follow_redirects=False)
        assert held.headers["location"].endswith("error=paused#youtube_shorts")
        assert "Posting is paused" in client.get("/posts").text

        client.post("/posting/pause", data={"stop": "no"})
        assert not posting.paused(db_path, 1)

        # the per-account daily cap: two a day, and the third waits for tomorrow
        third, _ = finished_channel_run(client, db_path, tmp)
        lines = run_auto(third)
        assert sum(1 for line in lines if "posted:" in line) == 1, lines  # one left under the cap of 2
        fourth, _ = finished_channel_run(client, db_path, tmp)
        lines = run_auto(fourth)
        assert all("limit of 2 posts" in line for line in lines if "held back" in line), lines
        assert FakeYouTube.sent == []


def test_a_channel_with_no_rights_record_never_posts_by_itself():
    import shutil

    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH")
        return
    from shorts.web import posting, worker

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {**CLIENT, "SHORTS_PROVIDER": "mock"}):
        client, db_path = signed_in(tmp)
        connect_youtube(client, channel_id="UCmine", handle="My channel")

        # auto_publish forced on in the database behind the page's back, with no rights on file
        run_id, source_id = finished_channel_run(client, db_path, tmp, rights="none", auto=1)
        assert worker.post_what_we_may(db_path, Path(tmp), run_id) == []
        assert posting.recent(db_path, 1) == []

        # nothing was posted, and the clips are there as ordinary drafts for a person
        assert "0 of 4 approved" in client.get("/runs/%s" % run_id).text
        conn = db.connect(db_path)
        states = [row[0] for row in conn.execute(
            "SELECT state FROM clip_versions WHERE run_id = ?", (run_id,))]
        conn.close()
        assert states and set(states) == {"draft"}, states

        # and the same channel with permission on file does post
        conn = db.connect(db_path)
        with conn:
            conn.execute("UPDATE channel_sources SET rights = 'permitted' WHERE id = ?", (source_id,))
        conn.close()
        source = __import__("shorts.web.channels", fromlist=["x"]).by_id(db_path, source_id)
        assert posting.held_back(db_path, 1, source, [], "UCmine") is None


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
