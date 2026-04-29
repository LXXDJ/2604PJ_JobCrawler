"""sites 테이블 CRUD."""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Optional
from urllib.parse import urlparse, urlunparse

from .db import get_conn


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    d = dict(row)
    d["sources"] = json.loads(d.get("sources") or "[]")
    return d


def _normalize_home_url(url: str) -> str:
    """origin-only home_url 은 항상 '/' 로 끝나게 통일."""
    p = urlparse(url)
    if not p.path:
        return urlunparse(p._replace(path="/"))
    return url


def upsert_site(
    site_id: str,
    home_url: str,
    *,
    name: Optional[str] = None,
    status: str = "pending",
    status_reason: Optional[str] = None,
    sources: Optional[list[dict]] = None,
) -> None:
    home_url = _normalize_home_url(home_url)
    sources_json = json.dumps(sources or [], ensure_ascii=False)
    with get_conn() as conn:
        conn.execute(
            """
            INSERT INTO sites (id, home_url, name, status, status_reason, sources)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                home_url      = excluded.home_url,
                name          = COALESCE(excluded.name, sites.name),
                status        = excluded.status,
                status_reason = excluded.status_reason,
                sources       = excluded.sources,
                updated_at    = datetime('now')
            """,
            (site_id, home_url, name, status, status_reason, sources_json),
        )


def get_site(site_id: str) -> Optional[dict[str, Any]]:
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM sites WHERE id = ?", (site_id,)).fetchone()
    return _row_to_dict(row) if row else None


def list_sites(status: Optional[str] = None) -> list[dict[str, Any]]:
    sql = "SELECT * FROM sites"
    params: tuple = ()
    if status:
        sql += " WHERE status = ?"
        params = (status,)
    sql += " ORDER BY id"
    with get_conn() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [_row_to_dict(r) for r in rows]


def update_status(
    site_id: str,
    status: str,
    *,
    reason: Optional[str] = None,
    redirect_to: Optional[str] = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE sites
               SET status = ?,
                   status_reason = ?,
                   redirect_to = COALESCE(?, redirect_to),
                   updated_at = datetime('now')
             WHERE id = ?
            """,
            (status, reason, redirect_to, site_id),
        )


def record_attempt(site_id: str, *, success: bool) -> None:
    with get_conn() as conn:
        if success:
            conn.execute(
                """
                UPDATE sites
                   SET consecutive_failures = 0,
                       last_success_at = datetime('now'),
                       last_attempt_at = datetime('now'),
                       updated_at      = datetime('now')
                 WHERE id = ?
                """,
                (site_id,),
            )
        else:
            conn.execute(
                """
                UPDATE sites
                   SET consecutive_failures = consecutive_failures + 1,
                       last_attempt_at = datetime('now'),
                       updated_at      = datetime('now')
                 WHERE id = ?
                """,
                (site_id,),
            )
