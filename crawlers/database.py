"""
SQLite DB 관리 모듈

모든 사이트의 구인구직 공고를 단일 테이블에 저장한다.
증분 수집을 위해 (source, external_id)로 중복을 체크한다.

사용 방법:
    from database import JobDatabase

    db = JobDatabase("data/jobs.db")
    db.init_schema()

    # 공고 저장 (이미 있으면 last_seen_at만 업데이트)
    result = db.upsert_job({
        "source": "camhr",
        "external_id": "10656655",
        "title": "Sales Executive",
        ...
    })
    # result = "inserted" or "updated"

    # 통계 조회
    stats = db.get_stats()
"""

import sqlite3
import json
from datetime import datetime
from contextlib import contextmanager


class JobDatabase:
    def __init__(self, db_path):
        self.db_path = db_path

    @contextmanager
    def connect(self):
        """DB 연결 컨텍스트 매니저 — with 문으로 자동 close"""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row  # 딕셔너리처럼 접근 가능
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ============================================================
    # 스키마 초기화
    # ============================================================

    def init_schema(self):
        """DB 테이블을 생성한다 (없으면)."""
        with self.connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,           -- 사이트 식별 (hanin, siemreap, camhr)
                    external_id TEXT NOT NULL,      -- 사이트별 공고 고유 ID
                    title TEXT NOT NULL,
                    company TEXT,                   -- 회사명 (또는 작성자)
                    location TEXT,                  -- 지역 (cities)
                    salary TEXT,                    -- 급여
                    job_type TEXT,                  -- Full Time, Part Time 등
                    pub_date TEXT,                  -- 게시일
                    link TEXT NOT NULL,             -- 원문 URL
                    content TEXT,                   -- 본문
                    raw_data TEXT,                  -- 사이트별 전체 JSON (확장용)

                    -- 증분 수집용 타임스탬프
                    first_seen_at TEXT NOT NULL,    -- 최초 수집일
                    last_seen_at TEXT NOT NULL,     -- 마지막 확인일

                    UNIQUE(source, external_id)
                );

                CREATE INDEX IF NOT EXISTS idx_source ON jobs(source);
                CREATE INDEX IF NOT EXISTS idx_first_seen ON jobs(first_seen_at);
                CREATE INDEX IF NOT EXISTS idx_last_seen ON jobs(last_seen_at);

                -- 크롤링 실행 로그 (어떤 사이트를 언제 얼마나 수집했는지)
                CREATE TABLE IF NOT EXISTS crawl_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    new_count INTEGER DEFAULT 0,        -- 신규 공고 수
                    updated_count INTEGER DEFAULT 0,    -- 기존 공고 재확인 수
                    error TEXT                          -- 에러 메시지 (있을 경우)
                );
            """)

    # ============================================================
    # Upsert (삽입 또는 업데이트)
    # ============================================================

    def upsert_job(self, job):
        """
        공고를 DB에 저장한다.
        - (source, external_id) 조합이 없으면 INSERT
        - 있으면 last_seen_at만 UPDATE

        Returns: "inserted" 또는 "updated"
        """
        now = datetime.now().isoformat()

        with self.connect() as conn:
            # 이미 존재하는지 확인
            row = conn.execute(
                "SELECT id FROM jobs WHERE source = ? AND external_id = ?",
                (job["source"], job["external_id"])
            ).fetchone()

            if row:
                # 업데이트: last_seen_at만 갱신
                conn.execute(
                    "UPDATE jobs SET last_seen_at = ? WHERE id = ?",
                    (now, row["id"])
                )
                return "updated"
            else:
                # 신규 삽입
                conn.execute("""
                    INSERT INTO jobs (
                        source, external_id, title, company, location, salary,
                        job_type, pub_date, link, content, raw_data,
                        first_seen_at, last_seen_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job["source"],
                    job["external_id"],
                    job.get("title", ""),
                    job.get("company", ""),
                    job.get("location", ""),
                    job.get("salary", ""),
                    job.get("job_type", ""),
                    job.get("pub_date", ""),
                    job.get("link", ""),
                    job.get("content", ""),
                    json.dumps(job.get("raw_data", {}), ensure_ascii=False),
                    now,
                    now,
                ))
                return "inserted"

    # ============================================================
    # 크롤링 실행 기록
    # ============================================================

    def start_crawl_run(self, source):
        """크롤링 시작 기록 — run_id 반환"""
        now = datetime.now().isoformat()
        with self.connect() as conn:
            cursor = conn.execute(
                "INSERT INTO crawl_runs (source, started_at) VALUES (?, ?)",
                (source, now)
            )
            return cursor.lastrowid

    def finish_crawl_run(self, run_id, new_count, updated_count, error=None):
        """크롤링 종료 기록"""
        now = datetime.now().isoformat()
        with self.connect() as conn:
            conn.execute("""
                UPDATE crawl_runs
                SET finished_at = ?, new_count = ?, updated_count = ?, error = ?
                WHERE id = ?
            """, (now, new_count, updated_count, error, run_id))

    # ============================================================
    # 조회
    # ============================================================

    def get_stats(self):
        """사이트별 공고 통계"""
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT source, COUNT(*) as count,
                       MIN(first_seen_at) as first_seen,
                       MAX(last_seen_at) as last_seen
                FROM jobs
                GROUP BY source
            """).fetchall()
            return [dict(r) for r in rows]

    def get_recent_runs(self, limit=10):
        """최근 크롤링 실행 이력"""
        with self.connect() as conn:
            rows = conn.execute("""
                SELECT * FROM crawl_runs
                ORDER BY started_at DESC
                LIMIT ?
            """, (limit,)).fetchall()
            return [dict(r) for r in rows]
