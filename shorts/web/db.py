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
    """
    -- one row per connected account, not per platform: a tester can hold several
    CREATE TABLE connections (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        platform TEXT NOT NULL,
        account_id TEXT NOT NULL,
        handle TEXT,
        -- sealed: a refresh token is a standing right to post as this person
        secret BLOB NOT NULL,
        status TEXT NOT NULL DEFAULT 'ok',
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        updated_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (user_id, platform, account_id)
    );
    ALTER TABLE clip_versions ADD COLUMN post_id TEXT;
    ALTER TABLE clip_versions ADD COLUMN post_url TEXT;
    ALTER TABLE clip_versions ADD COLUMN published_at TEXT;
    ALTER TABLE clip_versions ADD COLUMN publish_error TEXT;
    """,
    """
    -- a channel this tester may clip, and what says so
    CREATE TABLE channel_sources (
        id INTEGER PRIMARY KEY,
        user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        channel_id TEXT NOT NULL,
        url TEXT NOT NULL,
        title TEXT,
        -- owned: its id matches a connected account, checked here, never typed in.
        -- permitted: the owner said yes, and who/when/where is on file.
        -- none: clips go to review and nothing posts by itself.
        rights TEXT NOT NULL DEFAULT 'none',
        granted_by TEXT,
        granted_at TEXT,
        evidence TEXT,
        rules TEXT,
        attribution TEXT,
        auto_publish INTEGER NOT NULL DEFAULT 0,
        active INTEGER NOT NULL DEFAULT 1,
        paused_reason TEXT,
        last_video_id TEXT,
        last_checked_at TEXT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE (user_id, channel_id)
    );
    ALTER TABLE runs ADD COLUMN source_id INTEGER REFERENCES channel_sources(id) ON DELETE SET NULL;
    """,
    """
    -- which account a clip went to, and whether a person or the worker sent it
    ALTER TABLE clip_versions ADD COLUMN account_id TEXT;
    ALTER TABLE clip_versions ADD COLUMN published_by TEXT;
    -- one switch per tester that stops all posting, mid-run included
    ALTER TABLE users ADD COLUMN posting_paused_at TEXT;
    """,
    """
    -- how often to look at a channel; daily by default
    ALTER TABLE channel_sources ADD COLUMN cadence_minutes INTEGER NOT NULL DEFAULT 1440;
    -- one row per account a clip actually reached, because a clip can reach several
    CREATE TABLE posts (
        id INTEGER PRIMARY KEY,
        run_id TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
        clip_id TEXT NOT NULL,
        platform TEXT NOT NULL,
        account_id TEXT NOT NULL,
        post_id TEXT,
        post_url TEXT,
        error TEXT,
        published_at TEXT,
        published_by TEXT,
        UNIQUE (run_id, clip_id, platform, account_id)
    );
    INSERT INTO posts (run_id, clip_id, platform, account_id, post_id, post_url,
                       published_at, published_by)
        SELECT run_id, clip_id, platform, COALESCE(account_id, ''), post_id, post_url,
               published_at, COALESCE(published_by, 'tester')
        FROM clip_versions WHERE published_at IS NOT NULL;
    """,
    """
    -- what a run looks like when nobody fills in a form
    CREATE TABLE user_settings (
        user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        json TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT (datetime('now'))
    );
    """,
    """
    -- the formula behind a channel: topics, hook shapes, pace. Not its identity.
    ALTER TABLE channel_sources ADD COLUMN style_json TEXT;
    ALTER TABLE channel_sources ADD COLUMN style_notes TEXT;
    ALTER TABLE channel_sources ADD COLUMN style_learned_at TEXT;
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
