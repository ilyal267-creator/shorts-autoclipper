"""SQLite for the dashboard: one file under the data directory.

Schema changes are appended to MIGRATIONS and never edited once shipped; `PRAGMA user_version`
records how many have run. Each feature adds its own tables when it lands.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# ponytail: one SQLite file with a single writer at a time; move to Postgres if the dashboard
# ever serves more than a few dozen people at once.
MIGRATIONS = [
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        email TEXT NOT NULL UNIQUE COLLATE NOCASE,
        name TEXT,
        invited_at TEXT NOT NULL DEFAULT (datetime('now')),
        revoked_at TEXT,
        last_seen_at TEXT
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        expires_at TEXT NOT NULL
    );
    """,
]


def connect(path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(path: str | Path) -> int:
    """Bring the database up to date; returns the schema version it ends on."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    try:
        conn.execute("PRAGMA journal_mode = WAL")  # the worker writes while pages read
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
            with conn:
                conn.executescript("BEGIN;" + script + "PRAGMA user_version = %d;" % number)
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()
