"""jobhunt 전용 SQLite (data/jobhunt.db). 팀 프로젝트 crawler.db 와 분리."""
from __future__ import annotations

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parents[1] / "data" / "jobhunt.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS postings (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source             TEXT NOT NULL,          -- 'wanted' | 'jumpit' | ...
    external_id        TEXT NOT NULL,
    url                TEXT NOT NULL,

    title              TEXT,
    company            TEXT,
    company_industry   TEXT,
    location           TEXT,
    years_from         INTEGER,                -- 사이트가 표기한 경력 하한 (0 = 신입)
    years_to           INTEGER,                -- 상한 (원티드 100 = 무관)
    due_date           TEXT,                   -- NULL = 상시
    category_ids       TEXT,                   -- JSON list
    skill_tags         TEXT,                   -- JSON list (사이트 제공 태그)

    -- 상세 본문 (사이트가 섹션을 나눠주면 그대로 보존)
    intro              TEXT,
    main_tasks         TEXT,
    requirements       TEXT,
    preferred          TEXT,
    benefits           TEXT,

    status             TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
    matched            INTEGER NOT NULL DEFAULT 0, -- 프로필 1차 필터 통과 여부
    match_reason       TEXT,

    first_seen_at      TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    last_seen_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    closed_at          TEXT,
    detail_fetched_at  TEXT,
    notified_at        TEXT,

    UNIQUE (source, external_id)
);

CREATE INDEX IF NOT EXISTS idx_postings_matched ON postings(matched, status);


-- LLM 구조화 추출 + 적합도. 프로필이 바뀌면 이 테이블만 지우고 다시 돌리면 됨.
CREATE TABLE IF NOT EXISTS insights (
    posting_id         INTEGER PRIMARY KEY REFERENCES postings(id) ON DELETE CASCADE,
    model              TEXT,
    extracted_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),

    role_type          TEXT,       -- llm_app | ml_engineer | ai_fullstack | backend | data | research | other
    summary            TEXT,
    years_min          INTEGER,
    years_max          INTEGER,
    required_skills    TEXT,       -- JSON list (정규화된 영문 표기)
    preferred_skills   TEXT,       -- JSON list
    domain             TEXT,
    english_required   INTEGER,    -- 0/1
    salary_text        TEXT,

    fit_score          INTEGER,    -- 0~100
    fit_reason         TEXT,
    gaps               TEXT        -- JSON list: 내가 부족한 요건
);
"""


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn
