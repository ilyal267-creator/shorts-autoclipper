"""CLI: `python -m shorts run --config run.json`."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

from . import agent, config as config_mod, run as run_mod


def decode_env(data: bytes) -> str:
    """Read a .env whatever wrote it — deciding from the bytes, not from the byte-order mark.

    PowerShell's `>>` writes UTF-16. Editors write UTF-8, sometimes with a BOM. And Notepad
    was seen writing a UTF-16 BOM in front of plain 8-bit text, then appending a pasted line
    as real UTF-16 — one file, two encodings. Trusting the BOM turned all of it to noise.
    So: strip any BOM, and decode each line by what its bytes are. ASCII text in UTF-16 has
    a NUL beside every character; in UTF-8 it has none.
    """
    for bom in (b"\xff\xfe", b"\xfe\xff", b"\xef\xbb\xbf"):
        if data.startswith(bom):
            data = data[len(bom):]
            break
    # Whole-file UTF-16 puts a NUL at every other byte. Counting NULs overall is not enough:
    # a short file mixing one ASCII line with one UTF-16 line can clear that bar too.
    odd = data[1::2]
    if len(data) % 2 == 0 and odd and odd.count(0) >= 0.9 * len(odd):
        return data.decode("utf-16-le", errors="replace").replace("\r", "")

    lines = []
    for raw in data.split(b"\n"):
        raw = raw.lstrip(b"\x00")  # the half of a UTF-16 newline left on the next line
        raw = raw.replace(b"\r\x00", b"").replace(b"\r", b"")
        if b"\x00" in raw:
            lines.append(raw.decode("utf-16-le", errors="replace").strip("﻿\x00"))
        else:
            lines.append(raw.decode("utf-8", errors="replace"))
    return "\n".join(lines)


def load_env(path: str | Path = ".env") -> list[str]:
    """Read KEY=value lines into the environment. Real environment variables win.

    A malformed .env must never take the CLI down — it is a convenience, not the config.
    """
    source = Path(path)
    if not source.exists():
        return []
    try:
        text = decode_env(source.read_bytes())
    except OSError:
        return []
    loaded = []
    for line in text.splitlines():
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


def cmd_auth(args) -> int:
    from . import auth

    try:
        path = auth.authorize_youtube()
    except auth.AuthError as exc:
        print("sign-in failed: %s" % exc, file=sys.stderr)
        return 1
    print("Signed in to YouTube. Refresh token stored in %s (gitignored)." % path)
    return 0


def cmd_voices(args) -> int:
    from . import voice

    try:
        voices = voice.list_voices()
    except voice.VoiceError as exc:
        print("cannot list voices: %s" % exc, file=sys.stderr)
        return 1
    for v in voices:
        labels = ", ".join("%s" % value for value in v["labels"].values() if value)
        print("%-22s %-24s %-10s %s" % (v["voice_id"], v["name"][:24], v["category"], labels))
    print("\n%d voices. Put ids under voiceover.voices in the run config, one per platform." % len(voices))
    return 0


def _youtube_status() -> str:
    from . import auth

    if os.getenv("YOUTUBE_ACCESS_TOKEN"):
        return "access token from env"
    if auth.token_path().exists():
        return "signed in (%s)" % auth.token_path()
    return "not signed in — python -m shorts auth youtube"


def cmd_doctor(args) -> int:
    rows = [
        ("ffmpeg", bool(shutil.which("ffmpeg"))),
        ("ffprobe", bool(shutil.which("ffprobe"))),
        ("model provider", agent.provider()),
        ("api.anthropic.com", _api_reachable()),
        ("TIKTOK_ACCESS_TOKEN", bool(os.getenv("TIKTOK_ACCESS_TOKEN"))),
        ("IG_ACCESS_TOKEN", bool(os.getenv("IG_ACCESS_TOKEN"))),
        ("YouTube sign-in", _youtube_status()),
        ("ELEVENLABS_API_KEY", bool(os.getenv("ELEVENLABS_API_KEY"))),
    ]
    for name, value in rows:
        print("%-22s %s" % (name, value))
    return 0


def printable(*streams) -> None:
    """Make output survive a caption's emoji, without corrupting it.

    Redirected or piped: UTF-8, because stdout carries the run summary as JSON and a consumer
    expects to parse it — an escape like \\U0001f447 would be invalid JSON, not a fallback.
    A console: keep its own encoding and degrade to escapes, so a legacy code page shows
    something readable instead of raising UnicodeEncodeError mid-run.
    """
    for stream in streams:
        try:
            if stream.isatty():
                stream.reconfigure(errors="backslashreplace")
            else:
                stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass  # a StringIO under test, or a stream that refuses — printing still works


def main(argv=None) -> int:
    printable(sys.stdout, sys.stderr)
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

    sp = sub.add_parser("auth", help="sign in to a platform once, in the browser")
    sp.add_argument("platform", choices=["youtube"])
    sp.set_defaults(handler=cmd_auth)

    sp = sub.add_parser("voices", help="list the ElevenLabs voices your account can use")
    sp.set_defaults(handler=cmd_voices)

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
