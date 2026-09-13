"""python -m shorts.web serve | invite <email> | revoke <email> | testers"""

from __future__ import annotations

import argparse


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m shorts.web")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="run the dashboard")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    commands.add_parser("invite", help="let an email sign in").add_argument("email")
    commands.add_parser("revoke", help="stop an email signing in and end its sessions").add_argument("email")
    commands.add_parser("testers", help="list invited emails")
    args = parser.parse_args(argv)

    from ..__main__ import load_env
    from . import auth, db
    from .app import create_app, data_dir

    load_env()
    db_path = data_dir() / "shorts.db"

    if args.command == "serve":
        import uvicorn

        uvicorn.run(create_app(), host=args.host, port=args.port)
    elif args.command == "invite":
        db.migrate(db_path)
        print(auth.invite(db_path, args.email))
    elif args.command == "revoke":
        db.migrate(db_path)
        print(auth.revoke(db_path, args.email))
    elif args.command == "testers":
        db.migrate(db_path)
        conn = db.connect(db_path)
        for row in conn.execute("SELECT email, name, invited_at, revoked_at, last_seen_at FROM users ORDER BY invited_at"):
            state = "revoked %s" % row["revoked_at"] if row["revoked_at"] else "last seen %s" % (row["last_seen_at"] or "never")
            print("%-36s %-16s %s" % (row["email"], row["name"] or "", state))
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
