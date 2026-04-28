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
    closed_at     TEXT
    -- UNIQUE(site_id, external_id) 의도적으로 제거.
    -- 같은 source 안에서 같은 공고가 sticky/promoted 형태로 여러 슬롯에 노출되면
    -- 그 occurrence 마다 별개 row 로 적재 (사이트 라이브 카운트와 일치).
    -- cross-source dedup 은 application 레벨 (runner) 에서 처리.
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


def _migrate_jobs_drop_unique(conn: sqlite3.Connection) -> bool:
    """기존 jobs 테이블에 UNIQUE(site_id, external_id) 제약이 있으면 제거.
    SQLite 는 ALTER 로 제약 제거 불가 → 테이블 재생성.
    Returns: True 면 마이그레이션 수행됨.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='jobs'"
    ).fetchone()
    if not row or "UNIQUE" not in (row["sql"] or ""):
        return False

    conn.executescript("""
        PRAGMA foreign_keys = OFF;
        ALTER TABLE jobs RENAME TO jobs_old_unique;
        CREATE TABLE jobs (
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
            closed_at     TEXT
        );
        INSERT INTO jobs
            (id, site_id, external_id, url, title, company, deadline, posted_at,
             raw, content_hash, first_seen_at, last_seen_at, closed_at)
        SELECT id, site_id, external_id, url, title, company, deadline, posted_at,
             raw, content_hash, first_seen_at, last_seen_at, closed_at
          FROM jobs_old_unique;
        DROP TABLE jobs_old_unique;
        CREATE INDEX IF NOT EXISTS idx_jobs_site         ON jobs(site_id);
        CREATE INDEX IF NOT EXISTS idx_jobs_last_seen    ON jobs(last_seen_at);
        CREATE INDEX IF NOT EXISTS idx_jobs_closed       ON jobs(closed_at);
        PRAGMA foreign_keys = ON;
    """)
    return True


def init_db(db_path: Path = DB_PATH) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        # 기존 DB 의 UNIQUE 제약 제거 (마이그레이션)
        if _migrate_jobs_drop_unique(conn):
            print("[migration] jobs UNIQUE(site_id, external_id) 제거됨")


if __name__ == "__main__":
    init_db()
    print(f"DB initialized at {DB_PATH}")
