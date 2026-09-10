"""CLI: `python -m shorts run --config run.json`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys

from . import agent, config as config_mod, run as run_mod


def _load(args) -> config_mod.Config:
    raw = json.loads(open(args.config, encoding="utf-8").read())
    if args.video:
        raw["source_video"] = args.video
    if args.mode:
        raw["posting_mode"] = args.mode
    if args.out:
        raw["output_dir"] = args.out
    return config_mod.from_dict(raw)


def cmd_run(args) -> int:
    cfg = _load(args)
    summary = run_mod.execute(cfg)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    published = sum(
        1
        for clip in summary["clips"]
        for state in clip["platforms"].values()
        if state.get("status") in ("published", "scheduled")
    )
    print(
        "\n%d clip(s), %d destination(s) published or scheduled, %d flag(s) for review — see %s/summary.json"
        % (len(summary["clips"]), published, len(summary["flags_for_human_review"]), cfg.output_dir),
        file=sys.stderr,
    )
    return 1 if summary.get("error") or summary.get("halted") else 0


def cmd_drain(args) -> int:
    cfg = _load(args)
    done = run_mod.drain_queue(cfg)
    print(json.dumps(done, indent=2, ensure_ascii=False))
    return 0


def cmd_doctor(args) -> int:
    rows = [
        ("ffmpeg", bool(shutil.which("ffmpeg"))),
        ("ffprobe", bool(shutil.which("ffprobe"))),
        ("model provider", agent.provider()),
        ("TIKTOK_ACCESS_TOKEN", bool(os.getenv("TIKTOK_ACCESS_TOKEN"))),
        ("IG_ACCESS_TOKEN", bool(os.getenv("IG_ACCESS_TOKEN"))),
        ("YOUTUBE_ACCESS_TOKEN", bool(os.getenv("YOUTUBE_ACCESS_TOKEN"))),
    ]
    for name, value in rows:
        print("%-22s %s" % (name, value))
    return 0


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):  # the summary is UTF-8; Windows consoles often aren't
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="shorts", description="Shorts auto-clipper & publisher")
    sub = parser.add_subparsers(dest="command", required=True)

    for name, handler, help_text in (
        ("run", cmd_run, "cut, caption, and publish one source video"),
        ("drain", cmd_drain, "publish everything in the schedule queue that is due"),
    ):
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("--config", required=True)
        sp.add_argument("--video", help="override source_video")
        sp.add_argument("--mode", choices=["auto_publish", "schedule", "draft_for_approval"])
        sp.add_argument("--out", help="override output_dir")
        sp.set_defaults(handler=handler)

    sp = sub.add_parser("doctor", help="check tooling and credentials")
    sp.set_defaults(handler=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except config_mod.ConfigError as exc:
        print("cannot start: %s" % exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
