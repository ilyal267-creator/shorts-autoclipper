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
    """
    CREATE TABLE runs (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        -- uploaded -> queued -> running -> needs_review | failed | cancelled
        status TEXT NOT NULL,
        source_name TEXT NOT NULL,
        source_path TEXT NOT NULL,
        source_bytes INTEGER NOT NULL,
        probe_json TEXT NOT NULL,
        config_json TEXT,
        summary_json TEXT,
        stage TEXT,
        error TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        queued_at TEXT,
        started_at TEXT,
        finished_at TEXT
    );
    CREATE INDEX runs_by_user ON runs (user_id, created_at);
    CREATE INDEX runs_by_status ON runs (status, queued_at);
    """,
    """
    ALTER TABLE runs ADD COLUMN cancel_requested_at TEXT;
    CREATE TABLE run_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%d %H:%M:%f', 'now')),
        stage TEXT,
        message TEXT NOT NULL,
        data_json TEXT
    );
    CREATE INDEX run_events_by_run ON run_events (run_id, id);
    """,
    """
    -- one row per (clip, platform) draft: what the tester approved and the copy as they edited it
    CREATE TABLE clip_versions (
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        clip_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'draft',
        title TEXT,
        caption TEXT,
        hashtags_json TEXT NOT NULL DEFAULT '[]',
        edited_at TEXT,
        approved_at TEXT,
        PRIMARY KEY (run_id, clip_id, platform)
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
