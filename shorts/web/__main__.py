"""python -m shorts.web serve [--host 127.0.0.1] [--port 8000]"""

from __future__ import annotations

import argparse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m shorts.web")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)

    if args.command == "serve":
        import uvicorn

        from ..__main__ import load_env
        from .app import create_app

        load_env()
        uvicorn.run(create_app(), host=args.host, port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
