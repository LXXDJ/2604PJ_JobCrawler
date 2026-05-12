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

import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from ..extractors.external_id import extract_external_id
from ..infra.db import get_conn
from ..infra.jobs_repo import insert_job
from ..infra.sites_repo import get_site, record_attempt, update_status
from .detail_crawler import fetch_detail
from .list_crawler import crawl_list


# gnuboard / 한국 BBS 흔한 list 라벨 prefix/suffix — title 에서 제거
_TITLE_PREFIX_RE = re.compile(
    r"^(텍스트|파일첨부|첨부파일|이미지|동영상|공지|NEW|HOT|new|hot|N|H)\s+"
)
_TITLE_SUFFIX_RE = re.compile(
    r"\s*(댓글\s*\d+\s*개?|링크|URL|첨부|new|N|hot|H|"
    r"\(\s*\d+\s*\)|\[\s*\d+\s*\])\s*$",
    re.IGNORECASE,
)


def _clean_title(t: Optional[str]) -> Optional[str]:
    if not t:
        return t
    s = t.strip()
    # prefix 반복 제거 (e.g. "텍스트 파일첨부 ...")
    while True:
        m = _TITLE_PREFIX_RE.match(s)
        if not m:
            break
        s = s[m.end():]
    # suffix 반복 제거
    while True:
        m = _TITLE_SUFFIX_RE.search(s)
        if not m:
            break
        s = s[:m.start()].rstrip()
    return s.strip() or t


FAILURE_THRESHOLD_FOR_PAUSED = 3  # consecutive_failures 가 이 값 이상이면 paused (진단 필요)
FAILURE_THRESHOLD_FOR_DEAD = 7    # 더 누적되면 dead


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
    skipped_no_detail: int = 0  # detail 실패로 적재 생략된 row 수
    consecutive_failures: int = 0
    jobs_total: int = 0   # 이 run 종료 후 사이트의 jobs 누적 건수
    notes: list[str] = field(default_factory=list)

    @property
    def display_name(self) -> str:
        return self.site_name or self.site_id


class ConcurrentRunError(RuntimeError):
    """같은 site_id 의 다른 run 이 아직 진행 중일 때 발생."""


_RUN_LOCK_STALE_MINUTES = 60  # 이 시간 넘게 ended_at NULL 이면 좀비로 간주, 락 무시


def _start_run(site_id: str, kind: str = "batch") -> int:
    """동일 site 의 진행 중 run 이 있으면 ConcurrentRunError.
    BEGIN IMMEDIATE 로 race window 차단.
    """
    with get_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        active = conn.execute(
            """SELECT id, started_at FROM crawl_runs
                WHERE site_id = ?
                  AND ended_at IS NULL
                  AND started_at >= datetime('now', ?)
                ORDER BY id DESC LIMIT 1""",
            (site_id, f"-{_RUN_LOCK_STALE_MINUTES} minutes"),
        ).fetchone()
        if active:
            conn.rollback()
            raise ConcurrentRunError(
                f"site={site_id} 의 run id={active['id']} 진행 중 (started_at={active['started_at']})"
            )
        cur = conn.execute(
            "INSERT INTO crawl_runs (site_id, kind) VALUES (?, ?)",
            (site_id, kind),
        )
        run_id = int(cur.lastrowid)
        conn.commit()
        return run_id


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

    try:
        run_id = _start_run(site_id)
    except ConcurrentRunError as e:
        rep.error = f"concurrent run 차단: {e}"
        rep.consecutive_failures = site["consecutive_failures"]
        rep.jobs_total = _count_jobs(site_id)
        return rep
    already_seen = _existing_external_ids(site_id)  # DB 의 기존 공고 ID (cross-batch 증분)
    cross_source_seen: set[str] = set()  # 이번 batch 안에서 이전 source 가 본 detail_url
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
            from ..fetchers.api import MAX_PAGES, crawl_api
            schema = ApiSchema.from_dict(src["api_schema"])
            api_res = crawl_api(
                schema,
                already_seen_ids=already_seen,
                use_proxy=bool(src.get("use_proxy")),
                max_pages=int(src.get("max_pages") or MAX_PAGES),
            )
            if not api_res.ok:
                error_msgs.append(f"{url}: {api_res.error}")
                rep.notes.append(f"api fail: {url} ({api_res.error})")
                log(f"  [{site_id}]   api fail: {api_res.error}")
                continue
            any_source_ok = True
            rep.rows_seen += len(api_res.rows)
            rows_to_process = api_res.rows
            log(f"  [{site_id}]   api {api_res.pages_crawled} pages, {len(api_res.rows)} rows")
        elif fetcher == "naver_cafe":
            from ..fetchers.naver_cafe import crawl_cafe
            cafe_id = src.get("cafe_id")
            menu_id = src.get("menu_id")
            if not cafe_id or not menu_id:
                error_msgs.append(f"{url}: cafe_id/menu_id missing")
                rep.notes.append(f"naver_cafe meta missing: {url}")
                log(f"  [{site_id}]   naver_cafe meta missing")
                continue
            cafe_res = crawl_cafe(
                cafe_id, menu_id,
                already_seen_ids=already_seen,
                progress_cb=log,
            )
            if not cafe_res.ok:
                error_msgs.append(f"{url}: {cafe_res.error}")
                rep.notes.append(f"naver_cafe fail: {url} ({cafe_res.error})")
                log(f"  [{site_id}]   naver_cafe fail: {cafe_res.error}")
                continue
            any_source_ok = True
            rep.rows_seen += len(cafe_res.rows)
            rows_to_process = cafe_res.rows
            log(f"  [{site_id}]   naver_cafe {cafe_res.pages_crawled} pages, "
                f"{len(cafe_res.rows)} rows")
        elif fetcher == "wordpress":
            from ..fetchers.wordpress import crawl_rest_categories, crawl_kboard_sitemap
            mode = src.get("mode")
            base = src.get("base_url") or url
            if mode == "rest_categories":
                cid = src.get("category_id")
                if cid is None:
                    error_msgs.append(f"{url}: category_id missing")
                    log(f"  [{site_id}]   wordpress meta missing (category_id)")
                    continue
                wp_res = crawl_rest_categories(
                    base, cid, already_seen_ids=already_seen, progress_cb=log,
                )
            elif mode == "kboard_sitemap":
                sitemaps = src.get("sitemaps") or []
                if not sitemaps:
                    error_msgs.append(f"{url}: sitemaps missing")
                    log(f"  [{site_id}]   wordpress meta missing (sitemaps)")
                    continue
                wp_res = crawl_kboard_sitemap(
                    base, sitemaps, already_seen_ids=already_seen, progress_cb=log,
                )
            else:
                error_msgs.append(f"{url}: unknown wordpress mode={mode}")
                log(f"  [{site_id}]   wordpress unknown mode: {mode}")
                continue
            if not wp_res.ok:
                error_msgs.append(f"{url}: {wp_res.error}")
                rep.notes.append(f"wordpress fail: {url} ({wp_res.error})")
                log(f"  [{site_id}]   wordpress fail: {wp_res.error}")
                continue
            any_source_ok = True
            rep.rows_seen += len(wp_res.rows)
            rows_to_process = wp_res.rows
            log(f"  [{site_id}]   wordpress {mode} pages={wp_res.pages_crawled} rows={len(wp_res.rows)}")
        else:
            listing = crawl_list(
                url,
                fetcher=fetcher,
                already_seen_ids=already_seen,
                id_extractor=extract_external_id,
                progress_cb=log,
                use_proxy=bool(src.get("use_proxy")),
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

        progress_step = 25
        for j, row in enumerate(rows_to_process, 1):
            ext_id = extract_external_id(row.detail_url)

            # 이미 DB 에 있는 (이전 배치에서 본) 공고면 skip — 중복 적재 방지
            if ext_id in already_seen:
                continue

            # cross-source dedup — 다른 source 에 같은 공고 있으면 skip
            if row.detail_url in cross_source_seen:
                continue

            detail = None
            # SPA 라 detail HTML 이 의미 없는 사이트는 skip_detail=True (heykorean 등) 로 표시.
            skip_detail = bool(src.get("skip_detail"))
            if fetch_details and not skip_detail:
                if fetcher == "naver_cafe":
                    # 네이버 카페는 JSON detail API 사용 (회원전용 카페는 401 → list-only)
                    import re as _re
                    from ..fetchers.naver_cafe import fetch_article_detail as _nc_detail
                    m = _re.search(r"/articles/(\d+)", row.detail_url)
                    cafe_id = src.get("cafe_id")
                    if m and cafe_id:
                        detail = _nc_detail(cafe_id, m.group(1))
                elif fetcher == "api" and (src.get("api_schema") or {}).get("detail_api_url_template"):
                    # SPA 사이트 — detail JSON API 사용 (camhr 등)
                    import re as _re
                    from ..extractors.api_schema import ApiSchema
                    from ..fetchers.api import fetch_api_detail
                    schema = ApiSchema.from_dict(src["api_schema"])
                    # detail URL 에서 id 추출 — detail_url_template 의 placeholder 위치
                    if schema.detail_url_template:
                        prefix, suffix = schema.detail_url_template.split("{id}", 1)
                        if row.detail_url.startswith(prefix) and row.detail_url.endswith(suffix or ""):
                            job_id = row.detail_url[len(prefix):]
                            if suffix:
                                job_id = job_id[:-len(suffix)]
                            if job_id:
                                detail = fetch_api_detail(schema, job_id)
                else:
                    detail = fetch_detail(row.detail_url)
                if detail and not detail.ok:
                    rep.detail_errors += 1

            # detail fetch 했는데 실패하면 (회원전용 카페, 권한 없음, 404 등) row 자체를
            # 적재하지 않음 — 제목만 있는 row 는 가치 낮음. skip_detail 사이트나
            # fetch_details=False 일 때는 list-only 가 의도된 동작이므로 통과.
            if fetch_details and not skip_detail and not (detail and detail.ok):
                rep.skipped_no_detail += 1
                continue

            # list_title 우선 — detail <title> 이 사이트 공통 brand 인 경우
            # (e.g. mofa.go.kr "워킹홀리데이인포센터 | 재외동포청") 가 흔함.
            # list_title 이 비어있을 때만 detail.title 로 fallback.
            title = row.title or (detail.title if detail and detail.ok else None)
            title = _clean_title(title)
            raw = {
                "list_title": row.title,
                "detail_url": row.detail_url,
            }
            if detail and detail.ok:
                from ..infra.media_store import download as _media_dl

                images_stored = []
                for u in detail.images:
                    s = _media_dl(u, site_id, referer=row.detail_url)
                    images_stored.append(s.to_dict())
                attachments_stored = []
                for a in detail.attachments:
                    s = _media_dl(a["url"], site_id, referer=row.detail_url)
                    d = s.to_dict()
                    d["text"] = a.get("text", "")
                    d["ext_orig"] = a.get("ext", "")
                    attachments_stored.append(d)

                body_html_local = detail.body_html or ""
                if body_html_local:
                    for img in images_stored:
                        if img.get("local_path") and img.get("src"):
                            body_html_local = body_html_local.replace(
                                img["src"], "/" + img["local_path"]
                            )

                raw.update({
                    "snippet": detail.raw_text_snippet,
                    "body_html": detail.body_html,
                    "body_html_local": body_html_local,
                    "images": images_stored,
                    "links": detail.links,
                    "attachments": attachments_stored,
                    "iframes": detail.iframes,
                    "videos": detail.videos,
                    "emails": detail.emails,
                    "phones": detail.phones,
                    "tables": detail.tables,
                    "meta": detail.meta,
                    "jsonld": detail.jsonld,
                })
            else:
                raw["snippet"] = None
            insert_job(
                site_id=site_id,
                external_id=ext_id,
                url=row.detail_url,
                title=title,
                raw=raw,
            )
            rep.inserted += 1
            already_seen.add(ext_id)

            if j % progress_step == 0:
                log(f"  [{site_id}]   ... {j}/{len(rows_to_process)} inserted "
                    f"(+{rep.inserted})")

        # source 끝난 후 그 source 의 detail_url 들을 cross_source_seen 에 추가
        # (다음 source 가 같은 url 시도하면 skip)
        for row in rows_to_process:
            cross_source_seen.add(row.detail_url)

    rep.success = any_source_ok

    if any_source_ok:
        record_attempt(site_id, success=True)
        _finish_run(
            run_id, result="success" if not error_msgs else "partial",
            inserted=rep.inserted, updated=rep.updated, unchanged=rep.unchanged,
            closed=rep.closed, rows_seen=rep.rows_seen,
            error="; ".join(error_msgs) or None,
        )
        # status_reason 자동 갱신 — 매 배치마다 "총 N건 / M건 수집누락 (사유)" 형식.
        # 총 = 사이트가 광고하는 누적 공고수 (target_jobs).
        # 누락 = target - 현재 DB 적재 수.
        # 사유: list 단계 미수집 / detail 실패 등 분해.
        site_now = get_site(site_id)
        target = site_now and site_now.get("target_jobs")
        current = _count_jobs(site_id)
        reason: Optional[str] = None
        if target and target > 0:
            missing = max(0, target - current)
            sub_reasons: list[str] = []
            # list 단계 미수집 추정: rows_seen < target 면 list 가 다 못 본 것.
            # (rows_seen 은 fresh 모드에선 전체, incremental 에선 신규만 — 후자에선
            #  current 가 이미 누적이라 대부분 deficit 이 detail/사이트 변동 쪽)
            list_seen_fresh_estimate = max(rep.rows_seen, current)
            if list_seen_fresh_estimate < target:
                sub_reasons.append(
                    f"list 단계에서 {target - list_seen_fresh_estimate}건 미수집 "
                    f"(페이지네이션/접근 제한)"
                )
            if fetch_details and rep.skipped_no_detail > 0:
                sub_reasons.append(
                    f"detail 실패로 {rep.skipped_no_detail}건 제외 "
                    f"(회원전용/만료/404 등)"
                )
            if missing > 0:
                tail = f" ({', '.join(sub_reasons)})" if sub_reasons else ""
                reason = f"총 {target}건 / {missing}건 수집누락{tail}"
            else:
                reason = f"총 {target}건 / 0건 수집누락"
        elif fetch_details and rep.skipped_no_detail > 0:
            # target 없는 사이트 — detail 실패 카운트만 기록
            reason = (f"detail 실패로 {rep.skipped_no_detail}건 제외 "
                      f"(회원전용/만료/404 등)")
        update_status(site_id, "active", reason=reason)
    else:
        record_attempt(site_id, success=False)
        rep.error = "; ".join(error_msgs) or "all sources failed"
        _finish_run(
            run_id, result="failed",
            inserted=rep.inserted, updated=rep.updated, unchanged=rep.unchanged,
            closed=rep.closed, rows_seen=rep.rows_seen,
            error=rep.error,
        )
        # 임계 초과 시 dead → paused (진단 필요) 순서로 평가.
        # paused 의 reason 은 새 포맷 (총 N건 / M건 수집누락) 유지하되 상태만 paused.
        site_after = get_site(site_id)
        if site_after:
            cf = site_after["consecutive_failures"]
            if cf >= FAILURE_THRESHOLD_FOR_DEAD:
                update_status(site_id, "dead",
                              reason=f"consecutive_failures>={FAILURE_THRESHOLD_FOR_DEAD}")
            elif cf >= FAILURE_THRESHOLD_FOR_PAUSED:
                target = site_after.get("target_jobs")
                current = _count_jobs(site_id)
                cause = f"배치 {cf}회 연속 실패 — {rep.error or '원인 불명'} (진단 필요)"
                if target and target > 0:
                    missing = max(0, target - current)
                    paused_reason = (f"총 {target}건 / {missing}건 수집누락 ({cause})"
                                     if missing > 0 else
                                     f"총 {target}건 / 0건 수집누락 ({cause})")
                else:
                    paused_reason = cause
                update_status(site_id, "paused", reason=paused_reason)

    # 사후 상태 채우기
    site_final = get_site(site_id)
    if site_final:
        rep.consecutive_failures = site_final["consecutive_failures"]
        rep.site_name = site_final.get("name")
    rep.jobs_total = _count_jobs(site_id)
    return rep
