"""사이트 1개 batch 실행: sources 순회 → 리스트/상세 → upsert → status 갱신.

흐름:
  for source in site.sources:
      crawl_list(source.url) → rows
      for row in rows:
          detail = fetch_detail(row.detail_url)
          upsert_job(...)

  mark_closed(site_id, seen_external_ids)
  record_attempt(success/fail)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

from ..extractors.external_id import extract_external_id
from ..infra.db import get_conn
from ..infra.jobs_repo import upsert_job
from ..infra.sites_repo import get_site, record_attempt, update_status
from .detail_crawler import fetch_detail
from .list_crawler import crawl_list


FAILURE_THRESHOLD_FOR_DEAD = 7  # consecutive_failures 가 이 값 이상이면 dead


@dataclass
class SiteRunReport:
    site_id: str
    site_name: Optional[str] = None
    success: bool = False
    error: Optional[str] = None
    sources_crawled: int = 0
    rows_seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    closed: int = 0
    detail_errors: int = 0
    consecutive_failures: int = 0
    jobs_total: int = 0   # 이 run 종료 후 사이트의 jobs 누적 건수
    notes: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.site_name or self.site_id


def _start_run(site_id: str, kind: str = "batch") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO crawl_runs (site_id, kind) VALUES (?, ?)",
            (site_id, kind),
        )
        return int(cur.lastrowid)


def _finish_run(
    run_id: int,
    *,
    result: str,
    inserted: int,
    updated: int,
    unchanged: int,
    closed: int,
    rows_seen: int,
    error: Optional[str] = None,
) -> None:
    with get_conn() as conn:
        conn.execute(
            """
            UPDATE crawl_runs
               SET ended_at = datetime('now'),
                   result = ?,
                   jobs_added = ?,
                   jobs_updated = ?,
                   jobs_unchanged = ?,
                   jobs_closed = ?,
                   rows_seen = ?,
                   error = ?
             WHERE id = ?
            """,
            (result, inserted, updated, unchanged, closed, rows_seen, error, run_id),
        )


def _count_jobs(site_id: str) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE site_id = ?", (site_id,)
        ).fetchone()
    return int(row["n"]) if row else 0


def _existing_external_ids(site_id: str) -> set[str]:
    with get_conn() as conn:
        return {
            r["external_id"] for r in conn.execute(
                "SELECT external_id FROM jobs WHERE site_id = ?", (site_id,)
            ).fetchall()
        }


def run_site(
    site_id: str,
    *,
    fetch_details: bool = True,
    progress_cb: Optional[Callable[[str], None]] = None,
) -> SiteRunReport:
    log = progress_cb or (lambda _msg: None)

    site = get_site(site_id)
    rep = SiteRunReport(site_id=site_id, site_name=(site or {}).get("name"))
    if not site:
        rep.error = f"site not found: {site_id}"
        return rep
    if site["status"] != "active":
        rep.error = f"site not active (status={site['status']})"
        rep.consecutive_failures = site["consecutive_failures"]
        rep.jobs_total = _count_jobs(site_id)
        return rep

    sources = site.get("sources") or []
    if not sources:
        rep.error = "no sources"
        return rep

    run_id = _start_run(site_id)
    already_seen = _existing_external_ids(site_id)  # DB 의 기존 공고 ID
    any_source_ok = False
    error_msgs: list[str] = []

    for si, src in enumerate(sources, 1):
        url = src.get("url")
        if not url:
            continue
        rep.sources_crawled += 1
        fetcher = src.get("fetcher", "static")
        log(f"  [{site_id}] source {si}/{len(sources)} ({fetcher}) {url[:90]}")

        if fetcher == "api" and src.get("api_schema"):
            from ..extractors.api_schema import ApiSchema
            from ..fetchers.api import crawl_api
            schema = ApiSchema.from_dict(src["api_schema"])
            api_res = crawl_api(schema, already_seen_ids=already_seen)
            if not api_res.ok:
                error_msgs.append(f"{url}: {api_res.error}")
                rep.notes.append(f"api fail: {url} ({api_res.error})")
                log(f"  [{site_id}]   api fail: {api_res.error}")
                continue
            any_source_ok = True
            rep.rows_seen += len(api_res.rows)
            rows_to_process = api_res.rows
            log(f"  [{site_id}]   api {api_res.pages_crawled} pages, {len(api_res.rows)} rows")
        else:
            listing = crawl_list(
                url,
                fetcher=fetcher,
                already_seen_ids=already_seen,
                id_extractor=extract_external_id,
                progress_cb=log,
            )
            if not listing.ok:
                error_msgs.append(f"{url}: {listing.error}")
                rep.notes.append(f"list fail: {url} ({listing.error})")
                log(f"  [{site_id}]   list fail: {listing.error}")
                continue
            any_source_ok = True
            rep.rows_seen += len(listing.rows)
            rows_to_process = listing.rows
            log(f"  [{site_id}]   list {listing.pages_crawled} pages, {len(listing.rows)} rows")

        # 매 N 행마다 crawl_runs 진행 갱신 (대시보드 라이브 진행)
        progress_step = 25
        for j, row in enumerate(rows_to_process, 1):
            ext_id = extract_external_id(row.detail_url)

            detail = None
            if fetch_details:
                detail = fetch_detail(row.detail_url)
                if not detail.ok:
                    rep.detail_errors += 1

            title = (detail.title if detail and detail.ok else None) or row.title
            raw = {
                "list_title": row.title,
                "detail_url": row.detail_url,
                "snippet": detail.raw_text_snippet if detail and detail.ok else None,
            }
            outcome = upsert_job(
                site_id=site_id,
                external_id=ext_id,
                url=row.detail_url,
                title=title,
                raw=raw,
            )
            if outcome == "inserted":
                rep.inserted += 1
                already_seen.add(ext_id)
            elif outcome == "updated":
                rep.updated += 1
            else:
                rep.unchanged += 1

            if j % progress_step == 0:
                log(f"  [{site_id}]   ... {j}/{len(rows_to_process)} processed "
                    f"(+{rep.inserted} ~{rep.updated})")

    rep.success = any_source_ok

    if any_source_ok:
        record_attempt(site_id, success=True)
        _finish_run(
            run_id, result="success" if not error_msgs else "partial",
            inserted=rep.inserted, updated=rep.updated, unchanged=rep.unchanged,
            closed=rep.closed, rows_seen=rep.rows_seen,
            error="; ".join(error_msgs) or None,
        )
    else:
        record_attempt(site_id, success=False)
        rep.error = "; ".join(error_msgs) or "all sources failed"
        _finish_run(
            run_id, result="failed",
            inserted=rep.inserted, updated=rep.updated, unchanged=rep.unchanged,
            closed=rep.closed, rows_seen=rep.rows_seen,
            error=rep.error,
        )
        # 임계 초과 시 dead
        site_after = get_site(site_id)
        if site_after and site_after["consecutive_failures"] >= FAILURE_THRESHOLD_FOR_DEAD:
            update_status(site_id, "dead",
                          reason=f"consecutive_failures>={FAILURE_THRESHOLD_FOR_DEAD}")

    # 사후 상태 채우기
    site_final = get_site(site_id)
    if site_final:
        rep.consecutive_failures = site_final["consecutive_failures"]
        rep.site_name = site_final.get("name")
    rep.jobs_total = _count_jobs(site_id)
    return rep
