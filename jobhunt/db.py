"""jobhunt 전용 SQLite (~/jobhunt-data/jobhunt.db). 팀 프로젝트 crawler.db 와 분리."""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path

# 개인 데이터(DB·대시보드 HTML·로그)는 프로젝트 폴더 밖에 둔다 — 프로젝트가 OneDrive 안이라
# 배치가 DB 를 쓰는 중에 동기화가 파일을 건드리면 충돌·손상 위험 + 개인 데이터 클라우드 업로드 (2026-11-04 이전)
DATA_DIR = Path(os.environ.get("JOBHUNT_DATA_DIR") or Path.home() / "jobhunt-data")
LOG_DIR = DATA_DIR / "logs"
DB_PATH = DATA_DIR / "jobhunt.db"


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


-- LLM 구조화 추출 + 적합도. 경력·목표가 바뀌면 extract --refit, 보유 기술만 바뀌면 rescore.
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

    fit_score          INTEGER,    -- 0~100 최종 (fit.py: 업무 범위 + 기술 매칭)
    fit_reason         TEXT,       -- 업무 범위 판단 근거 (LLM)
    gaps               TEXT        -- JSON list: 내가 부족한 요건 (기술 갭 + scope_gaps)
);


-- 회사 정보 (사이트 제공 태그: 인원·연봉 백분위·설립·투자·퇴사율·복지). 회사당 1행, 주기적으로 갱신
CREATE TABLE IF NOT EXISTS companies (
    source             TEXT NOT NULL,
    external_id        TEXT NOT NULL,
    name               TEXT,
    founded_year       INTEGER,
    tags               TEXT,       -- JSON list (최신 태그 체계 우선, 구 체계에만 있는 항목 뒤에 덧붙임)
    open_positions     INTEGER,    -- 진행 중 공고 수 (채용 활발도)
    response_rate      REAL,       -- 지원 응답률 %
    fetched_at         TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    PRIMARY KEY (source, external_id)
);


-- 기술 표기 분류표 (fit.py) — 공고에서 추출된 기술 표기마다 "내가 가졌나" 를 한 번만 판단해 저장.
-- 보유 기술 섹션이 바뀌면(skills_hash 불일치) 다시 분류. pinned=1 은 직접 고정한 값이라 재분류 안 함
CREATE TABLE IF NOT EXISTS skill_labels (
    key                TEXT PRIMARY KEY,   -- fit.label_key (표기 차이 무시 키)
    label              TEXT NOT NULL,      -- 대표 표기
    verdict            TEXT CHECK (verdict IN ('has','partial','none','na')),   -- na = 기술 아님 (점수·갭 제외)
    pinned             INTEGER NOT NULL DEFAULT 0,
    skills_hash        TEXT,               -- 분류 당시 보유 기술 섹션 버전
    classified_at      TEXT
);


-- 커리어 관점 포지션 평가 (내 적합도와 별개 — "이 자리가 내 미래에 얼마나 도움이 되나")
-- 프로필과 무관하게 공고 자체를 평가하므로 프로필이 바뀌어도 다시 돌릴 필요 없음
CREATE TABLE IF NOT EXISTS career (
    posting_id         INTEGER PRIMARY KEY REFERENCES postings(id) ON DELETE CASCADE,
    model              TEXT,
    evaluated_at       TEXT NOT NULL DEFAULT (datetime('now','localtime')),
    depth              INTEGER,    -- 1~5 전문성 성장
    breadth            INTEGER,    -- 1~5 업무 범위 → PM·리드 경로
    ai_core            INTEGER,    -- 1~5 AI 가 회사·제품의 핵심인가
    benefits           INTEGER,    -- 1~5 보상·복지 (본문 기준)
    growth_note        TEXT,       -- 이 자리에서 기를 수 있는 것 한 줄
    reason             TEXT
);
"""

# 기존 DB 에 나중에 추가된 컬럼
_MIGRATIONS = [
    ("postings", "company_id", "TEXT"),
    ("postings", "body", "TEXT"),          # 섹션 구분 없는 사이트(사람인·잡코리아)의 본문 전체 텍스트
    ("postings", "body_images", "TEXT"),   # JSON list — 이미지 통짜 공고의 이미지 URL (OCR 대비 보존)
    ("postings", "dup_of", "INTEGER"),     # 다른 사이트에 같은 공고가 있으면 대표 공고 id (LLM 분석은 대표만)
    ("postings", "ocr_at", "TEXT"),        # 이미지 공고 OCR 시각 (결과는 body 에 합침, ocr.py)
    # 회사 규모·재무 (사람인: NICE·국민연금 / 잡코리아 회사 페이지). 원티드 회사는 사람인에서 이름+설립연도로 찾아 채움
    ("companies", "employees", "INTEGER"),
    ("companies", "revenue_eok", "REAL"),       # 매출액 (억원)
    ("companies", "op_income_eok", "REAL"),     # 영업이익 (억원)
    ("companies", "revenue_growth", "REAL"),    # 매출액 증가율 %
    ("companies", "company_type", "TEXT"),      # 대기업 / 중견기업 / 중소기업 ...
    ("companies", "fin_source", "TEXT"),        # 재무 출처 (예: "saramin:<csn>")
    ("companies", "fin_checked_at", "TEXT"),    # 재무 조회 시도 시각 (못 찾아도 찍음 → 30일 뒤 재시도)
    # 적합도 분리 (fit.py): fit_score·gaps 는 아래 값 + 보유 기술로 코드가 다시 계산
    ("insights", "scope_score", "INTEGER"),     # LLM 업무 범위 적합도 0~100 (기술 스택 제외)
    ("insights", "scope_gaps", "TEXT"),         # JSON list — LLM 기술 외 갭 (고정 라벨)
    ("insights", "skill_score", "INTEGER"),     # 코드 기술 매칭 0~100 (기술 표기 없으면 NULL)
    ("insights", "profile_hash", "TEXT"),       # 분석 당시 LLM 용 프로필 버전 (profile.profile_hash)
]


def get_conn(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30)   # 배치·서버가 동시에 쓰고 읽을 수 있어 잠금 대기 여유
    conn.row_factory = sqlite3.Row
    # WAL: 대시보드 서버 읽기와 배치 쓰기가 서로 막지 않게 (2026-11-03 수집·평가 동시 실행 중 database is locked)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    # skill_labels.verdict 에 'na' 추가 (2026-11-04) — CHECK 는 ALTER 로 못 바꿔 옮겨 담고 재생성
    old = conn.execute("SELECT sql FROM sqlite_master WHERE name = 'skill_labels'").fetchone()
    if old and "'na'" not in old["sql"]:
        conn.execute("ALTER TABLE skill_labels RENAME TO _skill_labels_old")
    conn.executescript(SCHEMA)
    if old and "'na'" not in old["sql"]:
        conn.executescript("INSERT INTO skill_labels SELECT * FROM _skill_labels_old; DROP TABLE _skill_labels_old;")
    for table, col, typ in _MIGRATIONS:
        if col not in {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
    return conn
