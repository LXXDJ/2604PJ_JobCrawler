"""jobs 테이블 적재.

- insert_job : 매번 INSERT (같은 source 안 sticky/promoted 같은 공고 multiple
  occurrence 도 별개 행으로 적재). 현재 사용중인 함수.
- upsert_job : 레거시 (UNIQUE 제약 시절). 더 이상 사용 X — 호환을 위해 보존.
- mark_closed: 사용 X (사용자 요구 — 사라진 공고 추적 안 함).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Iterable, Optional

from .db import get_conn


def insert_job(
    *,
    site_id: str,
    external_id: str,
    url: str,
    title: Optional[str] = None,
    company: Optional[str] = None,
    deadline: Optional[str] = None,
    posted_at: Optional[str] = None,
    raw: Optional[dict] = None,
) -> int:
    """매번 INSERT. UNIQUE 제약 없음 — 같은 (site_id, external_id) 도 별개 행.
    Returns: 새 row id.
    """
    raw_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None
    with get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO jobs
              (site_id, external_id, url, title, company, deadline, posted_at, raw)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (site_id, external_id, url, title, company, deadline, posted_at, raw_json),
        )
        return int(cur.lastrowid)


def _content_hash(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def upsert_job(
    *,
    site_id: str,
    external_id: str,
    url: str,
    title: Optional[str] = None,
    company: Optional[str] = None,
    deadline: Optional[str] = None,
    posted_at: Optional[str] = None,
    raw: Optional[dict] = None,
) -> str:
    """Returns 'inserted' | 'updated' | 'unchanged'."""
    payload = {
        "title": title,
        "company": company,
        "deadline": deadline,
        "posted_at": posted_at,
        "raw": raw,
    }
    chash = _content_hash(payload)
    raw_json = json.dumps(raw, ensure_ascii=False) if raw is not None else None

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id, content_hash FROM jobs WHERE site_id = ? AND external_id = ?",
            (site_id, external_id),
        ).fetchone()

        if existing is None:
            conn.execute(
                """
                INSERT INTO jobs
                  (site_id, external_id, url, title, company, deadline, posted_at,
                   raw, content_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (site_id, external_id, url, title, company, deadline, posted_at,
                 raw_json, chash),
            )
            return "inserted"

        if existing["content_hash"] == chash:
            conn.execute(
                "UPDATE jobs SET last_seen_at = datetime('now') WHERE id = ?",
                (existing["id"],),
            )
            return "unchanged"

        conn.execute(
            """
            UPDATE jobs
               SET url = ?, title = ?, company = ?, deadline = ?, posted_at = ?,
                   raw = ?, content_hash = ?, last_seen_at = datetime('now'),
                   closed_at = NULL
             WHERE id = ?
            """,
            (url, title, company, deadline, posted_at, raw_json, chash, existing["id"]),
        )
        return "updated"


def mark_closed(site_id: str, seen_external_ids: Iterable[str]) -> int:
    """이번 크롤에서 안 보인 공고를 closed 처리. 반환: closed 건수.

    SQLite 의 SQLITE_MAX_VARIABLE_NUMBER (기본 999) 제한 회피를 위해
    임시 테이블에 적재 후 NOT IN 서브쿼리로 처리.
    """
    seen = list({eid for eid in seen_external_ids if eid})
    with get_conn() as conn:
        conn.execute("CREATE TEMP TABLE IF NOT EXISTS _seen_eids (eid TEXT PRIMARY KEY)")
        conn.execute("DELETE FROM _seen_eids")
        if seen:
            conn.executemany("INSERT OR IGNORE INTO _seen_eids (eid) VALUES (?)",
                             [(e,) for e in seen])
        cur = conn.execute(
            """
            UPDATE jobs
               SET closed_at = datetime('now')
             WHERE site_id = ?
               AND closed_at IS NULL
               AND external_id NOT IN (SELECT eid FROM _seen_eids)
            """,
            (site_id,),
        )
        rows = cur.rowcount or 0
        conn.execute("DELETE FROM _seen_eids")
        return rows
