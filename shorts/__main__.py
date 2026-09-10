"""CLI: `python -m shorts run --config run.json`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import agent, config as config_mod, run as run_mod


def load_env(path: str | Path = ".env") -> list[str]:
    """Read KEY=value lines into the environment. Real environment variables win."""
    source = Path(path)
    if not source.exists():
        return []
    loaded = []
    for line in source.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        # Blank entries in a copied .env.example must not shadow a real variable.
        if value and key and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


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


TLS_HINT = (
    " — TLS is being intercepted (proxy or antivirus). Point SSL_CERT_FILE at that CA bundle "
    "for the model calls, and REQUESTS_CA_BUNDLE for the publishers"
)


def _reach(get, url: str) -> str:
    """A 401 counts as reachable — TLS worked and only the credential is missing."""
    try:
        return "ok (HTTP %d)" % get(url, timeout=15).status_code
    except Exception as exc:
        blob = (type(exc).__name__ + str(exc)).upper()
        return "FAILED: %s%s" % (type(exc).__name__, TLS_HINT if "SSL" in blob or "CERTIFICATE" in blob else "")


def _api_reachable() -> str:
    # httpx is what the Anthropic SDK uses, so check the path the run will actually take.
    try:
        import httpx

        return _reach(httpx.get, "https://api.anthropic.com/v1/models")
    except ImportError:
        pass
    try:
        import requests

        return _reach(requests.get, "https://api.anthropic.com/v1/models")
    except ImportError:
        return "unknown (neither httpx nor requests installed)"


def cmd_doctor(args) -> int:
    rows = [
        ("ffmpeg", bool(shutil.which("ffmpeg"))),
        ("ffprobe", bool(shutil.which("ffprobe"))),
        ("model provider", agent.provider()),
        ("api.anthropic.com", _api_reachable()),
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
    load_env(os.getenv("SHORTS_ENV_FILE", ".env"))
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
