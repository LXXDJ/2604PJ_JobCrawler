"""SQLite 연결 + 스키마 초기화."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[2] / "data" / "crawler.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
    id                    TEXT PRIMARY KEY,
    home_url              TEXT NOT NULL,
    name                  TEXT,

    status                TEXT NOT NULL DEFAULT 'pending'
                          CHECK (status IN ('pending','active','paused','dead')),
    status_reason         TEXT,

    sources               TEXT NOT NULL DEFAULT '[]',

    consecutive_failures  INTEGER NOT NULL DEFAULT 0,
    last_success_at       TEXT,
    last_attempt_at       TEXT,

    redirect_to           TEXT,

    created_at            TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_sites_status ON sites(status);


CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id       TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    external_id   TEXT NOT NULL,

    url           TEXT NOT NULL,
    title         TEXT,
    company       TEXT,
    deadline      TEXT,
    posted_at     TEXT,

    raw           TEXT,
    content_hash  TEXT,

    first_seen_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_seen_at  TEXT NOT NULL DEFAULT (datetime('now')),
    closed_at     TEXT,

    UNIQUE(site_id, external_id)
);

CREATE INDEX IF NOT EXISTS idx_jobs_site         ON jobs(site_id);
CREATE INDEX IF NOT EXISTS idx_jobs_last_seen    ON jobs(last_seen_at);
CREATE INDEX IF NOT EXISTS idx_jobs_closed       ON jobs(closed_at);


CREATE TABLE IF NOT EXISTS crawl_runs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    site_id       TEXT NOT NULL REFERENCES sites(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL CHECK (kind IN ('register','batch')),

    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    ended_at      TEXT,
    result        TEXT CHECK (result IN ('success','partial','failed')),

    jobs_added     INTEGER DEFAULT 0,
    jobs_updated   INTEGER DEFAULT 0,
    jobs_unchanged INTEGER DEFAULT 0,
    jobs_closed    INTEGER DEFAULT 0,
    rows_seen      INTEGER DEFAULT 0,

    error         TEXT
);

CREATE INDEX IF NOT EXISTS idx_runs_site_started ON crawl_runs(site_id, started_at DESC);
"""


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
