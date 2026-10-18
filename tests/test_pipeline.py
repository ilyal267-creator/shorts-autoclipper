"""Runnable checks for the logic that is wrong silently: cut maths, re-timing, slots, policy.

    python tests/test_pipeline.py        (or: pytest)
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shorts import compliance, config as config_mod, render  # noqa: E402
from shorts.clips import (  # noqa: E402
    Clip,
    duplicate_copy,
    keep_ranges,
    map_time,
    platform_notes,
    retime,
    subtitle_lines,
    validate,
)
from shorts.publish import PublishError, publish  # noqa: E402
from shorts.schedule import slots  # noqa: E402
from shorts.transcribe import Word, load_words  # noqa: E402

BASE_CONFIG = {
    "source_video": "in.mp4",
    "connected_accounts": [{"platform": "tiktok", "account_id": "acct_1", "handle": "@x"}],
    "rights_confirmed": True,
}


def test_keep_ranges_merges_overlapping_and_unsorted_cuts():
    assert keep_ranges(10, 20, []) == [(10.0, 20.0)]
    assert keep_ranges(10, 20, [(12, 13)]) == [(10.0, 12.0), (13.0, 20.0)]
    # out of order, overlapping, and one running past the end
    assert keep_ranges(10, 20, [(16, 19), (12, 13), (12.5, 14), (19.5, 25)]) == [
        (10.0, 12.0),
        (14.0, 16.0),
        (19.0, 19.5),
    ]
    assert keep_ranges(10, 20, [(5, 25)]) == []


def test_retime_shifts_words_after_a_cut_and_drops_cut_words():
    words = [Word("a", 10.0, 10.5), Word("gone", 12.2, 12.6), Word("b", 14.0, 14.5)]
    keeps = keep_ranges(10, 20, [(12, 13)])
    out = retime(words, keeps)
    assert [w.text for w in out] == ["a", "b"]
    assert out[0].start == 0.0
    # "b" sat 4.0s in, minus the 1.0s cut before it
    assert out[1].start == 3.0
    assert map_time(12.5, keeps) is None


def test_subtitle_lines_chunk_and_mark_emphasis():
    words = [Word("w%d" % i, i * 0.4, i * 0.4 + 0.3) for i in range(11)]
    words.append(Word("later", 8.0, 8.4))  # a pause longer than max_gap
    lines = subtitle_lines(words, emphasis=["w3"])
    assert all(len(line.text.split()) <= 6 for line in lines)  # a merge may add one word
    assert lines[-1].text == "later"
    assert any("w3" in line.emphasis for line in lines)


def test_tidy_lines_gives_every_line_time_to_be_read():
    from shorts.clips import Line, tidy_lines

    # a word the cut sliced off the front, then a normal line
    merged = tidy_lines([Line("Cisco?", 0.0, 0.10), Line("What's on When", 0.40, 2.54)], clip_end=3.0)
    assert len(merged) == 1
    assert merged[0].text == "Cisco? What's on When" and merged[0].start == 0.0

    # an isolated short word with room after it is stretched, never discarded
    stretched = tidy_lines([Line("later", 8.0, 8.4)], clip_end=20.0)
    assert stretched[0].text == "later" and stretched[0].end == 8.5

    # the leftover word at the clip's out-point has nowhere to go, so it goes
    orphan = tidy_lines([Line("I'm magic, man.", 7.5, 8.79), Line("I", 11.65, 11.77)], clip_end=11.77)
    assert [line.text for line in orphan] == ["I'm magic, man."]

    # emphasis survives a merge
    kept = tidy_lines([Line("all.", 5.7, 5.8, ["all."]), Line("Okay?", 5.9, 6.0)], clip_end=9.0)
    assert "all." in kept[0].emphasis


def test_merge_strays_reunites_a_word_with_its_phrase():
    from shorts.clips import Line, merge_strays

    # a word that closes the sentence joins the line before it
    back = merge_strays([Line("Look at me, look at", 4.5, 5.2), Line("me.", 5.2, 6.2)])
    assert [line.text for line in back] == ["Look at me, look at me."]
    assert back[0].end == 6.2

    # a word that does not close a sentence opens the line after it, across a pause
    forward = merge_strays([Line("What", 1.26, 2.22), Line("happened?", 3.08, 3.80)])
    assert [line.text for line in forward] == ["What happened?"]
    assert forward[0].start == 1.26

    # trailing off is not finishing: an ellipsis joins forward, a full stop does not
    ellipsis = merge_strays([Line("is...", 13.03, 13.87), Line("And that's that.", 15.63, 16.29)])
    assert [line.text for line in ellipsis] == ["is... And that's that."]
    full_stop = merge_strays([Line("Done.", 13.03, 13.87), Line("And that's that.", 15.63, 16.29)])
    assert len(full_stop) == 2

    # a word left hanging by the out-point goes, however long it is on screen
    cut_off = merge_strays(
        [Line("Oh going to die.", 8.4, 9.42), Line("What's", 14.91, 15.47)], clip_end=15.5
    )
    assert [line.text for line in cut_off] == ["Oh going to die."]
    # but a finished sentence at the end is content, not a fragment
    finished = merge_strays(
        [Line("Oh going to die.", 8.4, 9.42), Line("Wow.", 14.91, 15.47)], clip_end=15.5
    )
    assert len(finished) == 2
    # and a lone word is never dropped when it is the only line there is
    only = merge_strays([Line("What's", 0.0, 0.6)], clip_end=0.6)
    assert [line.text for line in only] == ["What's"]

    # a lone word with clear air after it was spoken that way, so it stays
    spoken = merge_strays([Line("Right.", 1.0, 2.0), Line("later", 8.0, 8.4)], clip_end=20.0)
    assert [line.text for line in spoken] == ["Right.", "later"]

    # a one-word answer with nothing adjacent stays as it is
    alone = merge_strays([Line("Yes.", 1.0, 2.0), Line("Much later on", 9.0, 10.0)])
    assert [line.text for line in alone] == ["Yes.", "Much later on"]

    # merging never builds an unreadable wall of text — 6 words is the ceiling (§4.4b)
    at_limit = merge_strays([Line("one two three four five", 0.0, 2.0), Line("six", 2.0, 2.5)])
    assert [line.text for line in at_limit] == ["one two three four five six"]
    over = merge_strays([Line("one two three four five six", 0.0, 2.0), Line("seven", 2.0, 2.5)])
    assert len(over) == 2


def test_timeline_shows_both_ends_and_names_the_silences():
    from shorts import agent
    from shorts.transcribe import Transcript

    words = [
        Word("hook", 0.0, 0.4), Word("line", 0.4, 0.9),
        Word("after", 3.2, 3.6), Word("the", 3.6, 3.8), Word("gap", 3.8, 4.2),
    ]
    rendered = agent.timeline(Transcript(words=words, language="en", duration=5.0, source="t"))
    assert "[0.0-0.9] hook line" in rendered
    assert "(silence 2.3s: 0.9-3.2)" in rendered  # the cuttable range, stated outright
    assert "[3.2-4.2] after the gap" in rendered

    # a pause too short to cut does not fragment the run
    tight = [Word("no", 0.0, 0.4), Word("break", 0.6, 1.0)]
    assert agent.timeline(Transcript(words=tight, language="en", duration=2.0, source="t")) == (
        "[0.0-1.0] no break"
    )


def test_lines_never_join_across_a_cut():
    from shorts.clips import Line, merge_strays, seams, tidy_lines

    # 10s kept, 2.4s removed, 4s kept -> the join lands at 10.0 on the clip timeline
    assert seams(keep_ranges(15.6, 29.5, [(16.9, 19.3)])) == [1.3]
    assert seams([(0.0, 10.0), (12.4, 16.4)]) == [10.0]
    assert seams([(0.0, 10.0)]) == []  # nothing removed, nothing to guard

    # "What's your" and "OK." sat seconds apart until the cut made them neighbours
    across = [Line("What's your", 9.2, 10.0), Line("OK.", 10.0, 10.6)]
    assert len(merge_strays(list(across), seam_points=[10.0])) == 2
    assert len(merge_strays(list(across))) == 1  # would have merged without the seam

    # the short-line rescue respects it too, rather than reaching over the cut
    stitched = tidy_lines(
        [Line("What's your", 9.2, 10.0), Line("OK.", 10.0, 10.2)], clip_end=14.0, seam_points=[10.0]
    )
    assert [line.text for line in stitched] == ["What's your", "OK."]

    # and chunking starts a new line at the seam instead of running through it
    words = [Word("what's", 9.2, 9.6), Word("your", 9.6, 10.0), Word("OK.", 10.0, 10.6)]
    assert [line.text for line in subtitle_lines(words, seam_points=[10.0])] == ["what's your", "OK."]


def test_snap_nudges_a_boundary_but_never_drags_it_across_silence():
    from shorts import agent
    from shorts.transcribe import Transcript

    words = [Word("a", 41.8, 42.1), Word("b", 50.6, 50.9)]  # then an 8s gap to the clip's end
    transcript = Transcript(words=words, language="en", duration=61.7, source="test")

    # within tolerance: pulled onto the word edge so the cut lands cleanly
    assert agent.snap(42.0, transcript, "start") == 41.8
    assert agent.snap(50.7, transcript, "end") == 50.9

    # beyond it: an out-point held past the last word survives, and so does the clip's length
    assert agent.snap(53.0, transcript, "end") == 53.0
    kept = 53.0 - agent.snap(41.8, transcript, "start")
    assert abs(kept - 11.2) < 1e-6  # the 11.2s the model sized, not 9.1s after a drag


def test_validate_rejects_short_clips_and_near_duplicates():
    a = Clip("clip_1", 0, 40, [], {}, "preserve", "", "high")
    b = Clip("clip_2", 5, 45, [], {}, "preserve", "", "high")
    short = Clip("clip_3", 0, 40, [(2, 35)], {}, "preserve", "", "high")
    assert validate(a, (15, 45), [a]) == []
    assert any("overlaps" in p for p in validate(a, (15, 45), [a, b]))
    assert any("floor" in p for p in validate(short, (15, 45), [short]))


def test_duplicate_copy_catches_cross_platform_reuse():
    copy = {
        "tiktok": {"hook": "Same hook", "caption": "one"},
        "instagram_reels": {"hook": "same HOOK", "caption": "two"},
    }
    assert any("hook" in problem for problem in duplicate_copy(copy))


def test_platform_notes_flag_drift_from_the_spec_table():
    good = {"caption": "tight caption", "hashtags": ["#a", "#b", "#c"], "title": ""}
    assert platform_notes("tiktok", good) == []
    assert any("hashtags" in n for n in platform_notes("tiktok", {**good, "hashtags": ["#a"]}))
    assert any("does not show" in n for n in platform_notes("tiktok", {**good, "title": "x"}))

    yt = {"caption": "c", "hashtags": ["#a"], "title": "T" * 101}
    assert any("truncated at 100" in n for n in platform_notes("youtube_shorts", yt))
    assert any("no title" in n for n in platform_notes("youtube_shorts", {**yt, "title": ""}))


def test_slots_respect_cadence_and_gap():
    now = datetime(2026, 9, 10, 10, 0, tzinfo=timezone.utc)
    schedule = {"times": ["09:00", "13:00", "18:00"], "per_platform_per_day": 1, "min_gap_minutes": 180, "timezone": "UTC"}
    picked = slots(schedule, 3, now)
    assert [s.isoformat() for s in picked] == [
        "2026-09-10T13:00:00+00:00",
        "2026-09-11T09:00:00+00:00",
        "2026-09-12T09:00:00+00:00",
    ]
    two_per_day = slots({**schedule, "per_platform_per_day": 2}, 2, now)
    assert [s.hour for s in two_per_day] == [13, 18]

    # a second clip for the same account must not stack on a slot already booked
    already = slots({**schedule, "per_platform_per_day": 3}, 1, now)
    assert slots({**schedule, "per_platform_per_day": 3}, 1, now, taken=already)[0].hour == 18


def test_config_defaults_and_hard_requirements():
    cfg = config_mod.from_dict(dict(BASE_CONFIG))
    assert cfg.posting_mode == "draft_for_approval"  # §8: never auto-publish unasked
    assert cfg.clip_count == 3 and cfg.clip_length_range == (15, 45)
    assert any("posting_mode" in flag for flag in cfg.flags)

    for missing in ("source_video", "connected_accounts"):
        raw = dict(BASE_CONFIG)
        raw.pop(missing)
        try:
            config_mod.from_dict(raw)
            raise AssertionError("expected ConfigError for missing %s" % missing)
        except config_mod.ConfigError:
            pass


def test_compliance_blocks_banned_words_unconfirmed_rights_and_halts():
    cfg = config_mod.from_dict({**BASE_CONFIG, "content_policy": {"banned_words": ["guaranteed"]}})
    copy = {"tiktok": {"caption": "This is guaranteed to work", "hashtags": ["#x"]}}
    verdict = compliance.check(cfg, "clean transcript", copy, {"verdict": "pass"})
    assert not verdict.ok and "banned word" in verdict.reasons[0]

    no_rights = config_mod.from_dict({**BASE_CONFIG, "rights_confirmed": False})
    assert not compliance.check(no_rights, "hi", {"tiktok": {"caption": "hi"}}, {"verdict": "pass"}).ok

    try:
        compliance.check(cfg, "hi", {"tiktok": {"caption": "hi"}}, {"verdict": "halt_run", "reason": "minor safety"})
        raise AssertionError("expected HaltRun")
    except compliance.HaltRun:
        pass


def test_publish_refuses_without_an_asset():
    try:
        publish("tiktok", "acct_1", "", {"caption": "hi"})
        raise AssertionError("expected PublishError")
    except PublishError:
        pass
    assert publish("tiktok", "acct_1", "x.mp4", {"caption": "hi"}, dry_run=True).status == "skipped"


def test_render_plan_matches_the_cut_list():
    clip = Clip("clip_1", 10, 40, [(20, 22)], {"mode": "center_crop"}, "preserve", "", "high")
    cmd = render.build_command(clip, "in.mp4", Path("out/clip_1.ass"), Path("out/clip_1.mp4"), 1920, 1080)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "concat=n=2" in graph  # two keep ranges either side of the cut
    assert "crop=607:1080" in graph  # 9:16 slice of a 1920x1080 source
    assert clip.duration == 28.0

    # A multi-track capture must be pinned by index, not left to ffmpeg's pick.
    assert "[0:a:0]atrim" in graph and "[0:v:0]trim" in graph
    track2 = render.build_command(
        clip, "in.mp4", Path("out/c.ass"), Path("out/c.mp4"), 1920, 1080, audio_track=2
    )
    assert "[0:a:2]atrim" in track2[track2.index("-filter_complex") + 1]

    lines = subtitle_lines([Word("boom", 0.0, 0.5)], emphasis=["boom"])
    ass = render.build_ass(lines, config_mod.DEFAULT_SUBTITLE_STYLE)
    assert r"{\c&H004DE1FF}boom{\r}" in ass  # highlight colour applied to the emphasis word
    assert ",424,1" in ass  # MarginV clears the bottom 20% safe zone

    # a merge can hand the same emphasis word in twice; highlighting must not nest
    from shorts.clips import Line

    doubled = render.build_ass(
        [Line("It's a bomb", 0.0, 1.0, ["bomb", "bomb"])], config_mod.DEFAULT_SUBTITLE_STYLE
    )
    assert r"{\c&H004DE1FF}{\c&H004DE1FF}" not in doubled
    assert doubled.count(r"{\c&H004DE1FF}bomb{\r}") == 1

    # the same word with and without punctuation is still one word, and must not nest
    repeated = render.build_ass(
        [Line("The biggest, biggest, biggest threat", 0.0, 2.0, ["biggest,", "biggest"])],
        config_mod.DEFAULT_SUBTITLE_STYLE,
    )
    assert r"{\c&H004DE1FF}{\c&H004DE1FF}" not in repeated
    assert repeated.count(r"{\c&H004DE1FF}") == 1  # first appearance only, not every repeat
    assert "biggest, biggest, biggest threat" in repeated.replace(r"{\c&H004DE1FF}", "").replace(
        r"{\r}", ""
    )  # the words themselves survive untouched


def test_audio_extract_pins_the_same_track_the_render_uses():
    from shorts import media

    cmd = media.audio_extract_cmd("in.mkv", 2, "out.wav")
    assert cmd[cmd.index("-map") + 1] == "0:a:2"  # not the decoder's own default pick
    assert "16000" in cmd and "pcm_s16le" in cmd  # what whisper wants, mono 16kHz

    clip = Clip("clip_1", 0, 20, [], {"mode": "center_crop"}, "preserve", "", "high")
    graph = render.build_command(
        clip, "in.mkv", Path("a.ass"), Path("o.mp4"), 1920, 1080, audio_track=2
    )
    filtergraph = graph[graph.index("-filter_complex") + 1]
    assert "[0:a:2]" in filtergraph  # transcript and render read the same stream


def test_load_env_reads_a_powershell_written_file():
    """`echo "K=v" >> .env` in PowerShell produces UTF-16; it must not crash the CLI."""
    import os
    import tempfile

    from shorts.__main__ import load_env

    with tempfile.TemporaryDirectory() as tmp:
        for label, encoding in (("UTF16", "utf-16"), ("UTF8BOM", "utf-8-sig"), ("PLAIN", "utf-8")):
            key = "SHORTS_TEST_%s" % label
            env_file = Path(tmp) / ("%s.env" % label)
            env_file.write_text("%s=value-%s\n" % (key, label), encoding=encoding)
            os.environ.pop(key, None)
            try:
                assert load_env(env_file) == [key], label
                assert os.environ[key] == "value-%s" % label, label
            finally:
                os.environ.pop(key, None)

        # What Notepad actually wrote on a real machine: a UTF-16 BOM in front of 8-bit text,
        # and on a second save a pasted line appended as genuine UTF-16. Trusting the BOM
        # decoded the lot as noise; every line here has to come back intact.
        from shorts.__main__ import decode_env

        hybrid = b"\xff\xfe" + b"FIRST=one\nSECOND=two\n"
        assert decode_env(hybrid).splitlines()[:2] == ["FIRST=one", "SECOND=two"]
        mixed = b"\xff\xfe" + b"FIRST=one\n" + "THIRD=three".encode("utf-16-le")
        assert decode_env(mixed).splitlines() == ["FIRST=one", "THIRD=three"]
        crlf16 = "﻿A=1\r\nB=2\r\n".encode("utf-16-le")  # PowerShell, Windows line ends
        assert decode_env(crlf16).splitlines()[:2] == ["A=1", "B=2"]

        # Undecodable bytes are survivable: no exception, and nothing bogus exported.
        broken = Path(tmp) / "broken.env"
        broken.write_bytes(b"\x80\x81\x82 not really text\n")
        assert load_env(broken) == []


def test_printable_keeps_piped_output_parseable_and_a_console_alive():
    import io

    from shorts.__main__ import printable

    class Stream(io.TextIOWrapper):
        def __init__(self, tty):
            super().__init__(io.BytesIO(), encoding="cp1252", errors="strict")
            self._tty = tty

        def isatty(self):
            return self._tty

    payload = '{"caption": "ship it 👇 — now"}'

    # Before the fix, cp1252 rejected the emoji outright.
    try:
        bare = Stream(tty=True)
        bare.write(payload)
        bare.flush()
        raise AssertionError("expected cp1252 to reject the emoji")
    except UnicodeEncodeError:
        pass

    # Piped: must stay valid UTF-8 JSON — an escape here would break the consumer.
    piped = Stream(tty=False)
    printable(piped)
    piped.write(payload)
    piped.flush()
    assert json.loads(piped.buffer.getvalue().decode("utf-8"))["caption"] == "ship it 👇 — now"

    # A console: never raise, degrade to an escape.
    console = Stream(tty=True)
    printable(console)
    console.write(payload)
    console.flush()
    shown = console.buffer.getvalue().decode("cp1252")
    assert "\\U0001f447" in shown and "—" in shown

    printable(None, object())  # streams that cannot reconfigure are ignored, not fatal


def test_load_env_does_not_shadow_real_variables():
    import os
    import tempfile

    from shorts.__main__ import load_env

    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text(
            "# a comment\n"
            "\n"
            'SHORTS_TEST_QUOTED="quoted value"\n'
            "SHORTS_TEST_PLAIN = plain\n"
            "SHORTS_TEST_BLANK=\n"
            "SHORTS_TEST_TAKEN=from-file\n"
            "not a pair\n",
            encoding="utf-8",
        )
        os.environ["SHORTS_TEST_TAKEN"] = "from-environment"
        os.environ.pop("SHORTS_TEST_BLANK", None)
        try:
            loaded = load_env(env_file)
            assert os.environ["SHORTS_TEST_QUOTED"] == "quoted value"
            assert os.environ["SHORTS_TEST_PLAIN"] == "plain"
            assert "SHORTS_TEST_BLANK" not in os.environ  # empty line must not set anything
            assert os.environ["SHORTS_TEST_TAKEN"] == "from-environment"  # real env wins
            assert "SHORTS_TEST_TAKEN" not in loaded
        finally:
            for key in ("SHORTS_TEST_QUOTED", "SHORTS_TEST_PLAIN", "SHORTS_TEST_TAKEN"):
                os.environ.pop(key, None)
    assert load_env(Path(tmp) / "gone.env") == []


def test_fit_keeps_the_whole_frame_and_the_config_can_force_it():
    clip = Clip("clip_1", 0, 10, [], {"mode": "fit"}, "preserve", "", "high")
    cmd = render.build_command(clip, "in.mp4", Path("a.ass"), Path("o.mp4"), 1280, 720)
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert "split=2[bg][fg]" in graph  # one copy for the fill, one for the picture
    assert "boxblur" in graph and "overlay=(W-w)/2:(H-h)/2" in graph
    assert "[fg]scale=1080:-2" in graph  # full width, height follows the source
    assert "crop=405:720" not in graph  # nothing sliced off the sides
    assert graph.count("subtitles=") == 1 and graph.endswith("[vout]")

    # crop modes are untouched by the change
    cropped = Clip("clip_1", 0, 10, [], {"mode": "center_crop"}, "preserve", "", "high")
    crop_cmd = render.build_command(cropped, "in.mp4", Path("a.ass"), Path("o.mp4"), 1280, 720)
    assert "crop=405:720" in crop_cmd[crop_cmd.index("-filter_complex") + 1]

    # whoever set up the run can see the picture; the override wins and bad values are refused
    assert config_mod.from_dict({**BASE_CONFIG, "reframe_mode": "fit"}).reframe_mode == "fit"
    try:
        config_mod.from_dict({**BASE_CONFIG, "reframe_mode": "letterbox"})
        raise AssertionError("expected ConfigError")
    except config_mod.ConfigError:
        pass


def test_youtube_sign_in_uses_pkce_and_asks_for_a_refresh_token():
    import base64
    import hashlib
    import os
    import tempfile
    import urllib.parse

    from shorts import auth

    verifier, challenge = auth.pkce_pair()
    assert 43 <= len(verifier) <= 128  # RFC 7636 bounds
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=")
    assert challenge == expected.decode()  # S256 of the verifier, never the verifier itself
    assert auth.pkce_pair()[0] != verifier  # fresh every sign-in

    client = {"auth_uri": "https://accounts.google.com/o/oauth2/auth", "client_id": "cid"}
    query = urllib.parse.parse_qs(
        urllib.parse.urlparse(auth.consent_url(client, "http://127.0.0.1:9/", challenge, "st")).query
    )
    assert query["access_type"] == ["offline"] and query["prompt"] == ["select_account consent"]
    assert query["code_challenge_method"] == ["S256"] and query["code_challenge"] == [challenge]
    assert query["scope"] == [auth.YOUTUBE_UPLOAD_SCOPE] and query["state"] == ["st"]
    assert "client_secret" not in query  # the secret only ever goes to the token endpoint

    # not signed in: a clear instruction, not a KeyError or a 401 from the upload
    with tempfile.TemporaryDirectory() as tmp:
        saved = {k: os.environ.pop(k, None) for k in ("YOUTUBE_ACCESS_TOKEN", "YOUTUBE_TOKEN_FILE")}
        os.environ["YOUTUBE_TOKEN_FILE"] = os.path.join(tmp, "none.json")
        auth._cached.clear()
        try:
            auth.youtube_access_token()
            raise AssertionError("expected AuthError")
        except auth.AuthError as exc:
            assert "shorts auth youtube" in str(exc)
        finally:
            os.environ.pop("YOUTUBE_TOKEN_FILE", None)
            for key, value in saved.items():
                if value is not None:
                    os.environ[key] = value


def test_probe_reads_a_phone_video_the_way_it_is_shown():
    import shutil
    import subprocess
    from unittest import mock

    from shorts import media

    # stored landscape, flagged to display portrait: size decisions must use the rotated shape
    assert media.displayed(3840, 2160, -90) == (2160, 3840)
    assert media.displayed(3840, 2160, 270.0) == (2160, 3840)
    assert media.displayed(1080, 1920, 180) == (1080, 1920)
    assert media.displayed(1920, 1080, 0) == (1920, 1080)

    # the banner an iPhone 15 .mov actually produces: HEVC, -90 rotation, aac + spatial "apac"
    banner = (
        "  Duration: 00:00:02.77, start: 0.000000, bitrate: 51629 kb/s\n"
        "  Stream #0:0[0x1](und): Video: hevc (Main 10) (hvc1 / 0x31637668), "
        "yuv420p10le(tv, bt2020nc/bt2020/arib-std-b67), 3840x2160, 50904 kb/s, 29.98 fps\n"
        "      displaymatrix: rotation of -90.00 degrees\n"
        "  Stream #0:1[0x2](und): Audio: aac (LC) (mp4a / 0x6134706D), 48000 Hz, stereo\n"
        "  Stream #0:2[0x3](und): Audio: none (apac / 0x63617061), 48000 Hz, 4.0, 413 kb/s\n"
    )
    fake = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=banner)
    with mock.patch.object(shutil, "which", side_effect=lambda b: None if b == "ffprobe" else b), \
            mock.patch.object(subprocess, "run", return_value=fake):
        probe = media.probe("IMG_2042.mov")

    assert (probe.width, probe.height) == (2160, 3840) and probe.is_vertical
    assert probe.audio_codecs == ["aac", "none"]
    assert probe.audio_tracks == 1  # the spatial track cannot be cut from
    assert probe.can_decode(0) and not probe.can_decode(1) and not probe.can_decode(5)
    assert probe.hdr  # arib-std-b67 is HLG: phones shoot HDR by default

    # an HDR source is tone-mapped to SDR before framing; an SDR one is left alone
    clip = Clip("clip_1", 0, 2, [], {"mode": "center_crop"}, "preserve", "", "high")
    hdr_cmd = render.build_command(clip, "i.mov", Path("a.ass"), Path("o.mp4"), 2160, 3840, hdr=True)
    graph = hdr_cmd[hdr_cmd.index("-filter_complex") + 1]
    assert "tonemap=tonemap=hable" in graph and "[vc]zscale=t=linear" in graph
    assert "[vsdr]crop=" in graph  # framing reads the tone-mapped picture, not the raw one
    sdr_cmd = render.build_command(clip, "i.mov", Path("a.ass"), Path("o.mp4"), 2160, 3840)
    assert "tonemap" not in sdr_cmd[sdr_cmd.index("-filter_complex") + 1]
    assert hdr_cmd[hdr_cmd.index("-color_trc") + 1] == "bt709"  # and it says what it is


def test_narration_is_timed_budgeted_and_mixed_over_the_clip():
    from shorts import agent, voice

    # ElevenLabs' per-character timings become per-word timings for the captions
    text = "Hook here. Payoff"
    starts = [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.1, 1.2, 1.3, 1.4, 1.5, 1.6]
    words = voice.words_from_alignment({
        "characters": list(text),
        "character_start_times_seconds": starts,
        "character_end_times_seconds": [s + 0.1 for s in starts],
    })
    assert [(w.text, w.start, round(w.end, 2)) for w in words] == [
        ("Hook", 0.0, 0.4), ("here.", 0.5, 1.0), ("Payoff", 1.1, 1.7)
    ]
    assert voice.words_from_alignment({}) == []  # no alignment: the caller falls back to whisper

    # whisper decides *when*, the script decides *what*: a mis-heard word never reaches the screen
    heard = [Word("Уелкам", 0.0, 0.5), Word("ту", 0.6, 0.8)]
    assert [(w.text, w.start) for w in voice.script_timed("Welcome to", heard)] == [
        ("Welcome", 0.0), ("to", 0.6)
    ]
    spread = voice.script_timed("one two three four", [Word("x", 1.0, 1.5), Word("y", 3.0, 5.0)])
    assert [w.text for w in spread] == ["one", "two", "three", "four"]
    assert spread[0].start == 1.0 and spread[-1].end <= 5.0  # inside the span actually spoken
    assert all(a.end <= b.start for a, b in zip(spread, spread[1:]))  # in order, no overlap

    # nothing configured: each platform gets its own default, with no call to the account
    from unittest import mock

    platforms = ["instagram_reels", "tiktok", "youtube_shorts"]
    with mock.patch.object(voice, "list_voices", side_effect=AssertionError("should not be needed")):
        assert voice.pick_voices(platforms, {}) == voice.DEFAULT_VOICES
        # a configured voice wins, and the others keep their defaults
        picked = voice.pick_voices(platforms, {"tiktok": "custom-id"})
        assert picked["tiktok"] == "custom-id"
        assert picked["youtube_shorts"] == voice.DEFAULT_VOICES["youtube_shorts"]
    # a default already taken by another platform is not reused; the account fills the gap
    liam = voice.DEFAULT_VOICES["tiktok"]
    with mock.patch.object(voice, "list_voices", return_value=[{"voice_id": liam}, {"voice_id": "spare"}]):
        picked = voice.pick_voices(platforms, {"instagram_reels": liam})
        assert picked["instagram_reels"] == liam and picked["tiktok"] == "spare"
        assert len(set(picked.values())) == 3  # never the same voice on two platforms

    # the script has to end before the clip does
    assert voice.word_budget(10.0) == 24 and voice.word_budget(0.5) == 4

    # narration only asked for when it is on
    assert "voiceover" in agent._copy_schema(True)["properties"]["tiktok"]["required"]
    assert "voiceover" not in agent._copy_schema(False)["properties"]["tiktok"]["required"]

    # the render takes the narration as a second input and ducks the clip's own sound under it
    clip = Clip("clip_1", 0, 10, [(4, 5)], {"mode": "center_crop"}, "preserve", "", "high")
    cmd = render.build_command(
        clip, "in.mp4", Path("a.ass"), Path("o.mp4"), 1920, 1080, voiceover=Path("vo.mp3")
    )
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert cmd.count("-i") == 2 and "vo.mp3" in cmd
    assert "[aorig]volume=-18.0dB[bed]" in graph and "[1:a:0]aresample=48000,apad[vo]" in graph
    assert "[bed][vo]amix=inputs=2:duration=first:normalize=0[aout]" in graph
    plain = render.build_command(clip, "in.mp4", Path("a.ass"), Path("o.mp4"), 1920, 1080)
    assert plain.count("-i") == 1 and "amix" not in plain[plain.index("-filter_complex") + 1]


def test_a_draft_publishes_later_with_the_account_it_is_given():
    import os
    import tempfile
    from unittest import mock

    from shorts import publish as publishing
    from shorts.run import publish_draft

    sent = []

    class Response:
        def __init__(self, body=None, headers=None):
            self.status_code, self.text = 200, "{}"
            self.headers, self._body = headers or {}, body or {}

        def json(self):
            return self._body

    class FakeRequests:
        def post(self, url, **kw):
            sent.append(("POST", url, kw))
            return Response(headers={"Location": "https://upload.example/session"})

        def put(self, url, **kw):
            sent.append(("PUT", url, kw))
            return Response({"id": "vid123"})

    with tempfile.TemporaryDirectory() as tmp:
        asset = Path(tmp) / "clip_1_youtube_shorts.mp4"
        asset.write_bytes(b"\0" * 64)
        entry = {
            "status": "draft", "asset_ref": str(asset), "hook": "This is about scope",
            "title": "Speed is scope", "caption": "edited on the review screen",
            "hashtags": ["#shorts"], "synthetic_media": True,
        }
        clean_env = {k: v for k, v in os.environ.items() if not k.startswith(("YOUTUBE_", "TIKTOK_", "IG_"))}
        with mock.patch.dict(os.environ, clean_env, clear=True), \
                mock.patch.object(publishing, "_requests", lambda: FakeRequests()), \
                mock.patch.object(publishing.time, "sleep", lambda s: None):
            result = publish_draft(entry, "youtube_shorts", "UC_tester_b", creds={"access_token": "tok-B"})
            assert result.status == "published" and result.post_id == "vid123", result
            init = sent[0][2]
            assert init["headers"]["Authorization"] == "Bearer tok-B"
            assert init["json"]["status"]["containsSyntheticMedia"] is True
            assert init["json"]["status"]["privacyStatus"] == "private"
            assert init["json"]["snippet"]["description"].startswith("edited on the review screen")

            # an unconnected account fails plainly and never falls back to another token
            sent.clear()
            refused = publish_draft(entry, "youtube_shorts", "UC_tester_c", creds={})
            assert refused.status == "failed" and "no youtube account connected" in refused.error
            assert sent == []


def test_hype_finds_the_loud_moment_and_leaves_a_flat_recording_alone():
    import shutil
    import subprocess
    import tempfile

    from shorts import hype

    # scoring, with no ffmpeg involved: a quiet room, one burst at 10-14s
    quiet = [-30.0] * 30
    loud = list(quiet)
    for second in range(10, 14):
        loud[second] = -12.0
    peaks = hype.find(loud)
    assert len(peaks) == 1, peaks
    assert (peaks[0].start, peaks[0].end) == (10.0, 14.0), peaks[0]
    assert "dB" in peaks[0].why
    assert hype.find(quiet) == []  # nothing stands out: say nothing rather than invent a peak
    assert hype.find([-30.0, -12.0]) == []  # a single second is a door slam, not a moment

    # what is said counts too: the same loudness, but someone reacting
    words = [Word("wow", 10.2, 10.5), Word("insane", 10.6, 11.0)]
    spoken = hype.find(loud, words)[0]
    assert "reaction" in spoken.why and spoken.score > peaks[0].score
    assert hype.lines([spoken]).startswith("(loud 10.0-14.0:")

    # a marker is filed among the speech at the right place in the timeline
    from shorts import agent
    from shorts.transcribe import Transcript

    transcript = Transcript(
        words=[Word("one", 1.0, 1.4), Word("two", 11.0, 11.4), Word("three", 20.0, 20.4)],
        language="en", duration=30.0, source="test",
    )
    timeline = agent.timeline(transcript, peaks=[hype.Peak(10.0, 14.0, 3.0, "+18dB")])
    rows = timeline.splitlines()
    assert any(row.startswith("(loud 10.0-14.0") for row in rows), rows
    starts = [agent._line_at(row) for row in rows]
    assert starts == sorted(starts), rows

    # and the reading itself, against a real file: 20s of quiet tone with a loud burst at 8-12s
    if not shutil.which("ffmpeg"):
        print("    (skipped the ffmpeg half: not on PATH)")
        return
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "burst.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:r=10:d=20",
             "-f", "lavfi", "-i", "sine=frequency=200:duration=20",
             "-f", "lavfi", "-i", "sine=frequency=600:duration=4",
             "-filter_complex",
             "[1:a]volume=-26dB[bed];[2:a]volume=0dB,adelay=8000|8000[hit];[bed][hit]amix=inputs=2:duration=first[a]",
             "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
             "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
            check=True,
        )
        heard = hype.of(str(path))
        assert heard, "a 4-second burst 14dB over the bed should be heard"
        assert abs(heard[0].start - 8.0) <= 1.0 and abs(heard[0].end - 12.0) <= 1.5, heard[0]


def test_watching_a_channel_resolves_dedupes_and_refuses_what_it_cannot_clip():
    from unittest import mock

    from shorts import sources

    feed_xml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns="http://www.w3.org/2005/Atom">'
        "<title>Ilya Builds</title>"
        "<entry><yt:videoId>vid_new</yt:videoId><title>Shipping on a Friday</title>"
        "<published>2026-10-18T09:00:00+00:00</published></entry>"
        "<entry><yt:videoId>vid_mid</yt:videoId><title>Cutting scope</title>"
        "<published>2026-10-17T09:00:00+00:00</published></entry>"
        "<entry><yt:videoId>vid_old</yt:videoId><title>The 2am bug</title>"
        "<published>2026-10-16T09:00:00+00:00</published></entry>"
        "</feed>"
    )
    # a real channel page name-drops other channels first: the decoy here is what the old
    # first-match-wins reading picked up, and it is not whose page this is
    channel_page = (
        '<html><script>{"recommended":"UCzzzzzzzzzzzzzzzzzzzzzz"}</script>'
        '<link rel="canonical" href="https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv"></html>'
    )

    # a bare id, a channel URL and an @handle all land on the same channel
    with mock.patch.object(sources, "_get", lambda url: feed_xml if "feeds" in url else channel_page):
        for given in ("UCabcdefghijklmnopqrstuv",
                      "https://www.youtube.com/channel/UCabcdefghijklmnopqrstuv",
                      "@ilyabuilds"):
            channel = sources.resolve(given)
            assert channel.channel_id == "UCabcdefghijklmnopqrstuv", given
            assert channel.title == "Ilya Builds"
        videos = sources.feed("UCabcdefghijklmnopqrstuv")
        assert [v.video_id for v in videos] == ["vid_new", "vid_mid", "vid_old"]
        assert videos[0].url.endswith("watch?v=vid_new") and videos[0].title == "Shipping on a Friday"

        # a page with no canonical id at all is refused, not guessed from a stray match
        with mock.patch.object(sources, "_get", lambda url: '<html>UCzzzzzzzzzzzzzzzzzzzzzz</html>'):
            try:
                sources.resolve("@mystery")
                raise AssertionError("a page with no canonical id should not resolve")
            except sources.SourceError as exc:
                assert "belongs to" in str(exc)

        for wrong in ("", "https://vimeo.com/12345"):
            try:
                sources.resolve(wrong)
                raise AssertionError("%r should not resolve" % wrong)
            except sources.SourceError:
                pass

    # a first check takes only the newest; after that, everything since, oldest first, once
    assert [v.video_id for v in sources.new_since(videos, None)] == ["vid_new"]
    assert [v.video_id for v in sources.new_since(videos, "vid_old")] == ["vid_mid", "vid_new"]
    assert sources.new_since(videos, "vid_new") == []

    # rate limiting is a pause, not an error to retry into
    class TooMany:
        status_code = 429
        text = ""

    with mock.patch.object(sources, "_requests", lambda: mock.Mock(get=lambda *a, **k: TooMany())):
        try:
            sources._get("https://www.youtube.com/feeds/videos.xml?channel_id=UC")
            raise AssertionError("429 should raise")
        except sources.SourceError as exc:
            assert exc.pause is True

    # what cannot be clipped is refused before any bandwidth is spent, each with its own reason
    for info, expected in (
        ({"is_live": True, "duration": 100}, "still live"),
        ({"live_status": "is_upcoming", "duration": 100}, "still live"),
        ({"availability": "subscriber_only", "duration": 100}, "not public"),
        ({"duration": 60 * 90}, "over the"),
        ({}, "how long"),
    ):
        try:
            sources.check(info, max_minutes=60)
            raise AssertionError("%s should be refused" % info)
        except sources.SourceError as exc:
            assert expected in str(exc), (info, str(exc))
    sources.check({"duration": 600, "availability": "public"}, max_minutes=60)  # this one is fine

    # YouTube deciding the server is a robot pauses the source and says how to get through
    try:
        sources._raise_for(RuntimeError("ERROR: Sign in to confirm you are not a bot"))
        raise AssertionError("a bot check should raise")
    except sources.SourceError as exc:
        assert exc.pause is True and "cookies" in str(exc).lower()
    try:
        sources._raise_for(RuntimeError("ERROR: Video unavailable"))
        raise AssertionError("an ordinary failure should raise")
    except sources.SourceError as exc:
        assert exc.pause is False

    # the download asks for a 1080p cap and writes source.<ext> where it was told to
    import tempfile

    asked = {}

    class FakeYDL:
        def __init__(self, options):
            asked.update(options)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def extract_info(self, url, download=False):
            return {"duration": 300, "availability": "public", "title": "Shipping on a Friday"}

        def download(self, urls):
            Path(asked["outtmpl"].replace("%(ext)s", "mp4")).write_bytes(b"video")

    with tempfile.TemporaryDirectory() as tmp, \
            mock.patch.object(sources, "_ydl", lambda extra=None: FakeYDL({**(extra or {})})):
        saved = sources.fetch("https://www.youtube.com/watch?v=vid_new", Path(tmp) / "run", 60)
        assert saved.name == "source.mp4" and saved.read_bytes() == b"video"
        assert "height<=1080" in asked["format"]


def test_a_video_with_no_speech_is_clipped_from_the_picture():
    import shutil
    import subprocess
    import tempfile

    from shorts import config as config_mod, frames, run as run_mod

    # frames are spread across the whole video, however long it is
    assert frames.every_for(10, max_frames=32) == frames.MIN_GAP
    assert 9.0 < frames.every_for(300, max_frames=32) < 10.0

    if not shutil.which("ffmpeg"):
        print("    (skipped the ffmpeg half: not on PATH)")
        return
    os.environ["SHORTS_PROVIDER"] = "mock"
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        source = work / "screen.mp4"
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=40",
             "-f", "lavfi", "-i", "anullsrc=channel_layout=mono:sample_rate=44100:duration=40",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-shortest", str(source)],
            check=True,
        )
        stills = frames.sample(str(source), 40.0, max_frames=8)
        assert 2 <= len(stills) <= 8, len(stills)
        assert stills[0].jpeg[:2] == b"\xff\xd8" and stills[0].at == 0.0
        assert stills[-1].at > stills[0].at

        cfg = config_mod.from_dict({
            "source_video": str(source),
            "connected_accounts": [{"platform": "tiktok", "account_id": "a", "handle": "@x"}],
            "clip_count": 2, "clip_length_range": [10, 20],
            "rights_confirmed": True, "output_dir": str(work / "out"),
        })
        summary = run_mod.execute(cfg)

        assert "error" not in summary, summary.get("error")
        assert len(summary["clips"]) == 2, summary["clips"]
        assert any("no speech in this source" in flag for flag in summary["flags_for_human_review"])
        assert any("no captions" in flag for flag in summary["flags_for_human_review"])
        for clip in summary["clips"]:
            assert Path(clip["asset_ref"]).exists()
            assert clip["platforms"]["tiktok"]["status"] == "draft"
        assert any("choosing from" in line and "frames" in line for line in summary["log"]), summary["log"]


def test_the_generator_is_mocked_by_default_and_costed_before_it_spends():
    import shutil
    import tempfile
    from unittest import mock

    from shorts import higgsfield

    # the estimate is linear in seconds and rises with resolution
    assert higgsfield.estimate(10, "480p") < higgsfield.estimate(10, "720p") < higgsfield.estimate(10, "1080p")
    assert higgsfield.estimate(6, "1080p") == round(higgsfield.rate_for("1080p") * 6, 4)
    assert higgsfield.estimate(6, "1080p", shots=3) == higgsfield.estimate(18, "1080p")
    with mock.patch.dict(os.environ, {"SHORTS_HF_RATE_1080P": "0.5"}):
        assert higgsfield.estimate(2, "1080p") == 1.0  # the operator can correct the guess

    # the ceiling refuses before anything is asked for, and says what it would have cost
    assert higgsfield.affordable(0.0, 1.0, ceiling=5.0) is None
    refused = higgsfield.affordable(4.80, 1.0, ceiling=5.0)
    assert refused and "$1.00" in refused and "$4.80" in refused
    assert higgsfield.affordable(99.0, 0.0, ceiling=5.0) is None  # nothing asked, nothing refused

    # a key alone does not start the spending: it takes a deliberate switch
    with mock.patch.object(higgsfield, "credentials", lambda: {"key_id": "a", "key_secret": "b"}):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SHORTS_GENERATOR", None)
            assert higgsfield.provider() == "mock"
        with mock.patch.dict(os.environ, {"SHORTS_GENERATOR": "real"}):
            assert higgsfield.provider() == "higgsfield"
    with mock.patch.object(higgsfield, "credentials", lambda: None), \
            mock.patch.dict(os.environ, {"SHORTS_GENERATOR": "real"}):
        assert higgsfield.provider() == "mock"  # asked for real, has no key: still spends nothing

    # tier 0 generates nothing at all, and says so rather than quietly costing money
    try:
        higgsfield.make_shot("a shot", Path("unused.mp4"), tier=0)
        raise AssertionError("tier 0 should refuse to generate")
    except higgsfield.GenerateError as exc:
        assert "generates nothing" in str(exc)

    if not shutil.which("ffmpeg"):
        print("    (skipped the mock-render half: ffmpeg not on PATH)")
        return
    from shorts import media

    with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"SHORTS_GENERATOR": "mock"}):
        shot = higgsfield.make_shot("a bowl of soup, slow push in", Path(tmp) / "shot_1.mp4", tier=1)
        assert shot.mocked and shot.cost == 0.0 and "nothing was spent" in shot.notes[0]
        probe = media.probe(str(shot.path))
        assert probe.height > probe.width, (probe.width, probe.height)  # 9:16, as a short must be
        assert abs(probe.duration - 4) < 1.0, probe.duration


def test_a_real_generation_polls_and_fails_honestly():
    import tempfile
    from unittest import mock

    from shorts import higgsfield

    creds = {"key_id": "id", "key_secret": "secret"}
    sent = {}

    class Reply:
        def __init__(self, status, body):
            self.status_code, self._body = status, body
            self.text = json.dumps(body)

        def json(self):
            return self._body

    def posting(url, headers=None, json=None, timeout=None):
        sent["body"], sent["auth"] = json, headers["Authorization"]
        return Reply(200, {"request_id": "req_1", "status_url": "https://api/status/req_1", "status": "queued"})

    states = iter([
        Reply(200, {"status": "queued"}),
        Reply(200, {"status": "in_progress"}),
        Reply(200, {"status": "completed", "video": {"url": "https://files/shot.mp4"}}),
    ])
    fake = mock.Mock(post=posting, get=lambda url, headers=None, timeout=None: next(states))

    with mock.patch.dict("sys.modules", {"requests": fake}), \
            mock.patch.object(higgsfield.time, "sleep", lambda s: None):
        handle = higgsfield.submit("a kitchen at dawn", 6, "1080p", {"image_urls": ["https://x/a.png"]}, creds)
        assert handle["request_id"] == "req_1"
        assert sent["auth"] == "Key id:secret"
        assert sent["body"]["aspect_ratio"] == "9:16" and sent["body"]["duration"] == 6
        assert sent["body"]["generate_audio"] is False  # our own narration goes over the top
        assert sent["body"]["image_urls"] == ["https://x/a.png"]
        assert higgsfield.wait_for(handle, creds) == "https://files/shot.mp4"

    # every way a shot can end badly says which, and nsfw is never retried into more spending
    for state, expected in (
        ({"status": "failed", "detail": "model unavailable"}, "failed"),
        ({"status": "nsfw"}, "nsfw"),
        ({"status": "completed"}, "no video on it"),
    ):
        once = mock.Mock(get=lambda url, headers=None, timeout=None: Reply(200, state))
        with mock.patch.dict("sys.modules", {"requests": once}), \
                mock.patch.object(higgsfield.time, "sleep", lambda s: None):
            try:
                higgsfield.wait_for({"status_url": "https://api/status/x"}, creds)
                raise AssertionError("%s should raise" % state)
            except higgsfield.GenerateError as exc:
                assert expected in str(exc), (state, str(exc))

    # a refused key is named as such, not reported as a broken shot
    refusing = mock.Mock(post=lambda *a, **k: Reply(401, {"detail": "Invalid credentials"}))
    with mock.patch.dict("sys.modules", {"requests": refusing}):
        try:
            higgsfield.submit("x", 6, "1080p", {}, creds)
            raise AssertionError("401 should raise")
        except higgsfield.GenerateError as exc:
            assert "refused the credentials" in str(exc)


def test_a_style_profile_is_a_formula_not_an_identity():
    from shorts import style

    class V:
        def __init__(self, title, published):
            self.title, self.published = title, published

    videos = [
        V("Wheels on the Camper | Family Camping Trip", "2026-10-18T09:00:00+00:00"),
        V("BIG vs Small Pumpkins! Halloween Counting Fun", "2026-10-16T09:00:00+00:00"),
        V("Fire Trucks for Kids | Play Pretend and Sing", "2026-10-11T09:00:00+00:00"),
        V("nothing parseable here", "not a date"),
    ]
    evidence = style.gather("Rhyme Town", videos, notes="  short songs, bright colours  ")
    assert evidence.titles[0].startswith("Wheels") and len(evidence.titles) == 4
    assert evidence.span_days == 7 and evidence.per_week == 3.0  # the unparseable date is skipped
    assert evidence.notes == "short songs, bright colours"

    # the guardrail: a profile that carries the modelled channel's identity is caught
    clean = {"topics": ["nursery rhymes"], "hook_shapes": ["a question, then a reversal"],
             "format": "song, three verses, payoff", "pacing": "40 seconds", "audience": "toddlers",
             "avoid": ["do not copy any character design"], "ideas": ["counting song about pumpkins"]}
    assert style.borrowed_identity(clean, "Rhyme Town") == []
    borrowed = {**clean, "ideas": ["a Rhyme Town style counting song"], "format": "like rhymetown does it"}
    caught = style.borrowed_identity(borrowed, "Rhyme Town", ["https://youtube.com/@rhymetown"])
    assert len(caught) == 2 and any("ideas" in c for c in caught) and any("format" in c for c in caught)

    # ordinary words in a channel name must not match everything
    assert style.borrowed_identity(clean, "The Daily Show Channel") == []
    # half a channel name is a category nobody owns: flagging it would flag every honest profile
    assert style.borrowed_identity(clean, "Rhymeworld - Nursery Rhymes for Kids") == []
    assert style.borrowed_identity({**clean, "topics": ["rhymeworld songs"]},
                                   "Rhymeworld - Nursery Rhymes for Kids")

    # with no model, the profile says so loudly rather than pretending to be a formula
    os.environ["SHORTS_PROVIDER"] = "mock"
    mocked = style.learn(evidence)
    assert "[MOCK]" in mocked["format"] and any("must not be used" in a for a in mocked["avoid"])
    assert set(mocked) == set(style.PROFILE_SCHEMA["required"])


def test_a_script_is_cut_to_fit_and_checked_before_it_is_generated():
    from shorts import script

    long_script = {
        "title": "t", "hook": "h", "narration": "word " * 100, "caption": "c", "hashtags": [],
        "why": "w",
        "shots": [{"prompt": "a cat at a laundry machine, wide, night", "seconds": n, "beat": "b"}
                  for n in (7, 5, 4, 4, 9, 9, 9)],
    }
    fitted = script.fit(dict(long_script), seconds=20)
    assert sum(s["seconds"] for s in fitted["shots"]) == fitted["seconds"] <= 20
    assert len(fitted["shots"]) <= script.MAX_SHOTS

    # a shot asking for more than the generator allows is clamped, not passed through
    odd = script.fit({"shots": [{"prompt": "x y z a b", "seconds": 99, "beat": "b"}]}, seconds=30)
    assert odd["shots"][0]["seconds"] == script.SHOT_SECONDS[1]
    assert script.fit({"shots": [{"prompt": "x y z a b", "seconds": 1, "beat": "b"}]}, 30)["shots"][0]["seconds"] == script.SHOT_SECONDS[0]

    # what a person is asked to look at before anything is spent
    assert script.problems(fitted), "100 words over 20 seconds should be flagged"
    assert any("rushed" in p for p in script.problems(fitted))
    assert script.problems({"shots": [], "narration": ""}) == ["the script has no shots to generate"]
    thin = {"shots": [{"prompt": "a cat", "seconds": 5, "beat": "b"}], "narration": "short", "seconds": 5}
    assert any("too thin" in p for p in script.problems(thin))
    good = {"shots": [{"prompt": "a basset hound at a washing machine, wide, night", "seconds": 6, "beat": "b"}],
            "narration": "Your cycle ended forty-one minutes ago.", "seconds": 6}
    assert script.problems(good) == []

    # the cost of a script is the cost of its shots, and a series is the sum of its videos
    assert script.cost_of(good, "1080p") == round(0.08 * 6, 4) or script.cost_of(good, "1080p") > 0
    series = {"videos": [good, good]}
    assert script.series_cost(series, "1080p") == round(2 * script.cost_of(good, "1080p"), 4)

    # with no model, a series says so in every video rather than looking like work
    os.environ["SHORTS_PROVIDER"] = "mock"
    mocked = script.write_series({}, count=3, seconds=20)
    assert len(mocked["videos"]) == 3
    assert all("[MOCK]" in v["narration"] and "must not be published" in v["narration"] for v in mocked["videos"])


def test_load_words_accepts_whisper_dumps():
    words = load_words({"segments": [{"words": [{"word": " hi ", "start": 0, "end": 0.4}]}]})
    assert words[0].text == "hi"


def main() -> int:
    tests = [value for name, value in sorted(globals().items()) if name.startswith("test_")]
    for test in tests:
        test()
        print("ok  %s" % test.__name__)
    print("\n%d checks passed" % len(tests))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
