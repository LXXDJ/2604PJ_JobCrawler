"""원티드 — 내부 JSON API 직접 사용 (인증 불필요).

  리스트: GET /api/v4/jobs?tag_type_ids=518&job_sort=job.latest_order&limit=100&offset=N
          → data[], links.next (마지막 페이지면 None)
  상세:   GET /api/v4/jobs/{id} → job.detail{intro, main_tasks, requirements, preferred_points, benefits}
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Iterator

from curl_cffi import requests

BASE = "https://www.wanted.co.kr"
DEV_CATEGORY = 518          # 개발 직군 전체
PAGE_SIZE = 100
_HEADERS = {"Referer": f"{BASE}/wdlist/{DEV_CATEGORY}"}


@dataclass
class ListItem:
    external_id: str
    url: str
    title: str
    company: str
    company_industry: str | None
    location: str | None
    years_from: int | None
    years_to: int | None
    due_date: str | None
    category_ids: list[int]


_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    # 상세 수백 건을 연달아 받으므로 TLS 세션 재사용
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session(impersonate="chrome124")
    return _SESSION


# 403 도 재시도 — 연속 요청 시 일시 차단됐다가 잠시 후 풀림 (2026-10-30 확인)
_RETRY_STATUS = (403, 429, 500, 502, 503, 504)


def _get_json(s: requests.Session, url: str, params: dict | None = None, retries: int = 4) -> dict:
    for attempt in range(retries):
        try:
            r = s.get(url, params=params, headers=_HEADERS, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code not in _RETRY_STATUS or attempt == retries - 1:
                r.raise_for_status()
        except requests.RequestsError:
            if attempt == retries - 1:
                raise
        time.sleep(5 * 2 ** attempt)
    raise RuntimeError(f"wanted: retries exhausted for {url}")


def iter_list(sleep_sec: float = 0.5, max_pages: int | None = None) -> Iterator[ListItem]:
    """개발 직군 전체 공고를 최신순으로 끝까지 순회."""
    s = _session()
    offset = 0
    page = 0
    while True:
        d = _get_json(s, f"{BASE}/api/v4/jobs", {
            "country": "kr", "tag_type_ids": DEV_CATEGORY, "job_sort": "job.latest_order",
            "locations": "all", "years": -1, "limit": PAGE_SIZE, "offset": offset,
        })
        for j in d.get("data") or []:
            addr = j.get("address") or {}
            company = j.get("company") or {}
            yield ListItem(
                external_id=str(j["id"]),
                url=f"{BASE}/wd/{j['id']}",
                title=j.get("position") or "",
                company=company.get("name") or "",
                company_industry=company.get("industry_name"),
                location=" ".join(x for x in (addr.get("location"), addr.get("district")) if x) or None,
                years_from=j.get("annual_from"),
                years_to=j.get("annual_to"),
                due_date=j.get("due_time"),
                category_ids=[c["id"] for c in j.get("category_tags") or []],
            )
        page += 1
        if not (d.get("links") or {}).get("next") or (max_pages and page >= max_pages):
            return
        offset += PAGE_SIZE
        time.sleep(sleep_sec)


def fetch_detail(external_id: str) -> dict:
    """상세 본문 섹션 + 기술 태그. 마감/삭제된 공고는 status 로 판별."""
    s = _session()
    job = _get_json(s, f"{BASE}/api/v4/jobs/{external_id}")["job"]
    detail = job.get("detail") or {}
    return {
        "intro": detail.get("intro"),
        "main_tasks": detail.get("main_tasks"),
        "requirements": detail.get("requirements"),
        "preferred": detail.get("preferred_points"),
        "benefits": detail.get("benefits"),
        "skill_tags": [t.get("text") for t in job.get("skill_tags") or [] if t.get("text")],
        "status": job.get("status"),
    }
