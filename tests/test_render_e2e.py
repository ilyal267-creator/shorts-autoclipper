"""End-to-end render check: generates a source video, runs the pipeline, inspects the output.

Needs ffmpeg on PATH; skips itself when there isn't one. Everything else is stdlib, and the
run uses the mock provider so no API key and no network are involved.

    python tests/test_render_e2e.py
    SHORTS_E2E_OUT=./render-check python tests/test_render_e2e.py   # keep what it rendered
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from shorts import config as config_mod, media, run as run_mod  # noqa: E402

SOURCE_SECONDS = 40
VOCAB = (
    "here is the thing nobody tells you about shipping software fast it is not about typing "
    "quicker it is about deleting the work you never needed to do in the first place"
).split()


def make_source(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "testsrc=size=1920x1080:rate=30:duration=%d" % SOURCE_SECONDS,
            "-f", "lavfi", "-i", "sine=frequency=440:duration=%d" % SOURCE_SECONDS,
            "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest", str(path),
        ],
        check=True,
    )


def make_transcript(path: Path) -> None:
    words, clock, index = [], 0.0, 0
    while clock < SOURCE_SECONDS - 1:
        words.append(
            {"text": VOCAB[index % len(VOCAB)], "start": round(clock, 2), "end": round(clock + 0.35, 2)}
        )
        clock += 0.4 if index % 9 else 1.1
        index += 1
    path.write_text(
        json.dumps({"language": "en", "duration": float(SOURCE_SECONDS), "words": words}),
        encoding="utf-8",
    )


@contextlib.contextmanager
def workspace():
    """A scratch dir, or SHORTS_E2E_OUT when you want to keep and look at the clips."""
    override = os.getenv("SHORTS_E2E_OUT")
    if override:
        path = Path(override).resolve()
        path.mkdir(parents=True, exist_ok=True)
        yield path
    else:
        with tempfile.TemporaryDirectory() as tmp:
            yield Path(tmp)


def test_render_end_to_end():
    if not shutil.which("ffmpeg"):
        print("SKIP: ffmpeg not on PATH — nothing was rendered or checked")
        return False

    os.environ["SHORTS_PROVIDER"] = "mock"
    with workspace() as work:
        source, transcript = work / "source.mp4", work / "transcript.json"
        make_source(source)
        make_transcript(transcript)

        cfg = config_mod.from_dict(
            {
                "source_video": str(source),
                "source_transcript": str(transcript),
                "connected_accounts": [
                    {"platform": "tiktok", "account_id": "acct_tt", "handle": "@demo"},
                    {"platform": "youtube_shorts", "account_id": "acct_yt", "handle": "@demo"},
                ],
                "clip_count": 2,
                "clip_length_range": [10, 30],
                "niche_keywords": ["devtools"],
                "rights_confirmed": True,
                "posting_mode": "draft_for_approval",
                "output_dir": str(work / "out"),
            }
        )
        summary = run_mod.execute(cfg)

        assert "error" not in summary, summary.get("error")
        assert len(summary["clips"]) == 2, summary["clips"]

        for clip in summary["clips"]:
            asset = Path(clip["asset_ref"])
            assert asset.exists() and asset.stat().st_size > 0, asset
            probe = media.probe(str(asset))
            assert (probe.width, probe.height) == (1080, 1920), (probe.width, probe.height)
            assert abs(probe.duration - clip["duration"]) < 1.0, (probe.duration, clip["duration"])

            subs = asset.with_suffix(".ass").read_text(encoding="utf-8")
            assert "Dialogue:" in subs, "no burned subtitle events for %s" % clip["clip_id"]

            # draft_for_approval must not have called a publisher (§4.7)
            assert {state["status"] for state in clip["platforms"].values()} == {"draft"}

        assert any("MOCK" in flag for flag in summary["flags_for_human_review"])
        assert (Path(cfg.output_dir) / "summary.json").exists()
        print("ok  rendered %d clips at 1080x1920 with burned captions" % len(summary["clips"]))
        if os.getenv("SHORTS_E2E_OUT"):
            print("    kept in %s" % work)
        return True


if __name__ == "__main__":
    print("\n1 check passed" if test_render_end_to_end() else "\nskipped — install ffmpeg to run it")
