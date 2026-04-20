"""
Embedded JSON 크롤러

config.extraction_method == "embedded_json" 인 사이트를 처리한다.
HTML <script> 에서 SSR 상태 JSON 을 뽑고, item_path 로 공고 배열에 도달한 뒤,
각 아이템에서 필드 휴리스틱으로 title/company/url 등을 추출.

Phase 2 범위:
    - requires_render = false 만 (requests 로 HTML fetch)
    - <script id="__NEXT_DATA__"> 같이 innerText 가 순수 JSON 인 경우만
    - pagination.type = "url_param" 만 (페이지마다 URL 재요청)
"""

import json
import time
from typing import Any, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from database import JobDatabase
from http_client import fetch


# 공고 아이템 내부 필드명 휴리스틱.
# field_mapping 을 config 에 넣지 않은 경우 이 후보 목록이 fallback.
TITLE_KEYS = [
    "title", "jobTitle", "postSubject", "subject", "name",
    "jobName", "jobPostingName", "position", "positionName",
]
COMPANY_KEYS = [
    "company", "companyName", "compNm", "giupNm", "corpName",
    "employer", "employerName", "orgName",
]
URL_KEYS = [
    "url", "link", "detailUrl", "jobUrl", "detail_url",
    "permalink", "path", "href",
]
DATE_KEYS = [
    "pubdate", "publishedAt", "postedAt", "date", "regDate",
    "registeredAt", "createdAt", "openDate",
]
ID_KEYS = [
    "id", "jobId", "postId", "seq", "seqNo", "articleId",
]
LOCATION_KEYS = [
    "location", "locationName", "region", "regionName", "area",
]
SALARY_KEYS = [
    "salary", "salaryText", "wage", "pay",
]


# ============================================================
# 상태 추출
# ============================================================

def _extract_state(html: str, script_selector: str) -> dict:
    """HTML 에서 script 찾아 내용 JSON 파싱."""
    soup = BeautifulSoup(html, "lxml")
    tag = soup.select_one(script_selector)
    if not tag or not tag.string:
        raise ValueError(f"script_selector {script_selector!r} 으로 스크립트 못 찾음")
    try:
        return json.loads(tag.string.strip())
    except json.JSONDecodeError as e:
        raise ValueError(f"스크립트 내용 JSON 파싱 실패: {e}") from e


def _traverse_path(state: Any, path: str) -> Any:
    """dot-notation path 로 state 내부 값 도달. 실패 시 None."""
    if not path:
        return state
    cur = state
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            return None
    return cur


# ============================================================
# 아이템 → job dict 매핑
# ============================================================

def _pick(item: dict, keys: list) -> str:
    """
    item 에서 keys 중 첫 유효값 꺼내기.
    값이 dict 이면 name/text/value 같은 내부 필드를 한 단계 내려 시도.
    """
    for k in keys:
        if k not in item:
            continue
        v = item[k]
        if v is None or v == "":
            continue
        if isinstance(v, (str, int, float)):
            s = str(v).strip()
            if s:
                return s
        if isinstance(v, dict):
            for inner in ("name", "text", "value", "title"):
                if inner in v and v[inner]:
                    return str(v[inner]).strip()
    return ""


def _build_job(item: dict, site_id: str, base_url: str) -> Optional[dict]:
    """단일 아이템을 JobDatabase 가 기대하는 모양으로 변환. 실패 시 None."""
    title = _pick(item, TITLE_KEYS)
    if not title:
        return None

    detail_url = _pick(item, URL_KEYS)
    if detail_url and not detail_url.startswith(("http://", "https://")) and base_url:
        detail_url = urljoin(base_url.rstrip("/") + "/", detail_url.lstrip("/"))

    ext_id = _pick(item, ID_KEYS) or detail_url or title

    return {
        "source": site_id,
        "external_id": str(ext_id),
        "title": title,
        "company": _pick(item, COMPANY_KEYS),
        "location": _pick(item, LOCATION_KEYS),
        "salary": _pick(item, SALARY_KEYS),
        "job_type": "",
        "pub_date": _pick(item, DATE_KEYS),
        "link": detail_url,
        "content": "",
        "raw_data": {"list": item},
    }


# ============================================================
# 메인 크롤링 진입점
# ============================================================

def crawl(
    site_id: str,
    config: dict,
    db_path: str,
    max_pages: Optional[int] = None,
    http_config: Optional[dict] = None,
):
    """
    Embedded JSON 기반 사이트 크롤링 + DB 저장.

    config 기대 모양:
        {
            "extraction_method": "embedded_json",
            "requires_render": false,
            "source": {
                "list_url": "...",
                "list_params": {...},
                "base_url": "...",
                "script_selector": "script#__NEXT_DATA__",
                "item_path": "props.pageProps.jobs",
            },
            "pagination": {"type": "url_param", "param": "page", "start": 1}
        }
    """
    if config.get("requires_render"):
        raise NotImplementedError(
            "requires_render=True 는 Phase 2.5 에서 지원 예정 "
            "(현재는 raw HTML 의 SSR state 만 처리)"
        )

    source = config.get("source") or {}
    pagination = config.get("pagination") or {}

    list_url = source.get("list_url")
    script_selector = source.get("script_selector", "script#__NEXT_DATA__")
    item_path = source.get("item_path", "")
    base_url = source.get("base_url", "")

    if not list_url:
        raise ValueError(f"[{site_id}] source.list_url 비어있음")

    http_kwargs = {
        "timeout": (http_config or {}).get("timeout", 30),
        "max_retries": (http_config or {}).get("max_retries", 3),
        "retry_backoff": (http_config or {}).get("retry_backoff", 2.0),
    }

    print("=" * 60)
    print(f"[{site_id}] Embedded JSON 크롤러 시작")
    print(f"    list_url        : {list_url}")
    print(f"    script_selector : {script_selector}")
    print(f"    item_path       : {item_path}")
    print("=" * 60)

    db = JobDatabase(db_path)
    db.init_schema()
    run_id = db.start_crawl_run(site_id)

    new_count = 0
    updated_count = 0

    try:
        start_page = pagination.get("start", 1)
        cap = max_pages or pagination.get("max_pages") or 1
        end_page = start_page + cap - 1

        print(f"\n    수집할 페이지: {start_page} ~ {end_page}")

        for page in range(start_page, end_page + 1):
            params = dict(source.get("list_params") or {})
            if pagination.get("type") == "url_param":
                params[pagination.get("param", "page")] = page

            html = fetch(list_url, params=params, **http_kwargs)
            try:
                state = _extract_state(html, script_selector)
            except ValueError as e:
                print(f"    [WARN] page={page} state 추출 실패: {e}")
                break

            items = _traverse_path(state, item_path)
            if not isinstance(items, list):
                print(
                    f"    [WARN] page={page} item_path={item_path!r} 가 배열 아님 "
                    f"(type={type(items).__name__}) — 중단"
                )
                break

            print(f"\n[{page}/{end_page}] {len(items)}건 처리 중...")
            if not items:
                # 빈 페이지가 나오면 다음 페이지도 거의 빈 편 → 중단
                print("    (빈 페이지 — 중단)")
                break

            for item in items:
                if not isinstance(item, dict):
                    continue
                job = _build_job(item, site_id, base_url)
                if job is None:
                    continue

                existing = _check_exists(db, site_id, job["external_id"])
                if existing:
                    db.upsert_job(job)
                    updated_count += 1
                else:
                    db.upsert_job(job)
                    new_count += 1
                    print(f"      [NEW] {job['title'][:60]}")

            print(f"    누적: 신규 {new_count}, 기존 {updated_count}")

            if page < end_page:
                time.sleep(1)

        db.finish_crawl_run(run_id, new_count, updated_count)

    except Exception as e:
        db.finish_crawl_run(run_id, new_count, updated_count, error=str(e))
        raise

    print(f"\n{'=' * 60}")
    print(f"[{site_id}] 크롤링 완료 — 신규 {new_count}건 / 재확인 {updated_count}건")
    print(f"{'=' * 60}")

    return {"new": new_count, "updated": updated_count}


def _check_exists(db, source: str, external_id: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        ).fetchone()
        return row is not None
