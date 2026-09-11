"""Runnable checks for the logic that is wrong silently: cut maths, re-timing, slots, policy.

    python tests/test_pipeline.py        (or: pytest)
"""

from __future__ import annotations

import json
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
