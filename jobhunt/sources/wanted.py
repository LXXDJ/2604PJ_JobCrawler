"""원티드 — 내부 JSON API 직접 사용 (인증 불필요).

어댑터 공통 인터페이스 (saramin / jobkorea 도 동일):
  SOURCE, FULL_LIST, iter_list(known_ids=...), fetch_detail(external_id) -> dict

  리스트: GET /api/v4/jobs?tag_type_ids=518&job_sort=job.latest_order&limit=100&offset=N
          → data[], links.next (마지막 페이지면 None)
  상세:   GET /api/v4/jobs/{id} → job.detail{intro, main_tasks, requirements, preferred_points, benefits}
"""
from __future__ import annotations

import time
from typing import Iterator

from curl_cffi import requests

from . import base
from .base import ListItem

SOURCE = "wanted"
FULL_LIST = True            # 개발 직군 리스트를 끝까지 돌므로 "리스트에서 사라짐 = 마감" 판단 가능
BASE = "https://www.wanted.co.kr"
DEV_CATEGORY = 518          # 개발 직군 전체
AI_CATEGORY = 1634          # 머신러닝 엔지니어 (AI 직무 대부분이 여기 태깅됨)
PAGE_SIZE = 100
_HEADERS = {"Referer": f"{BASE}/wdlist/{DEV_CATEGORY}"}

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    # 상세 수백 건을 연달아 받으므로 TLS 세션 재사용
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session(impersonate="chrome124")
    return _SESSION


def _get_json(s: requests.Session, url: str, params: dict | None = None) -> dict:
    return base.get(s, url, params=params, headers=_HEADERS).json()


def iter_list(sleep_sec: float = 0.5, max_pages: int | None = None, known_ids=None) -> Iterator[ListItem]:
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
            cats = [c["id"] for c in j.get("category_tags") or []]
            yield ListItem(
                external_id=str(j["id"]),
                url=f"{BASE}/wd/{j['id']}",
                title=j.get("position") or "",
                company=company.get("name") or "",
                company_id=str(company["id"]) if company.get("id") else None,
                company_industry=company.get("industry_name"),
                location=" ".join(x for x in (addr.get("location"), addr.get("district")) if x) or None,
                years_from=j.get("annual_from"),
                years_to=j.get("annual_to"),
                due_date=j.get("due_time"),
                category_ids=cats,
                ai_category=AI_CATEGORY in cats,
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
        "body": None,           # 원티드는 섹션이 나뉘어 와서 통짜 본문 불필요
        "images": [],
        "status": job.get("status"),
    }


def fetch_company(company_id: str) -> dict:
    """회사 태그(인원·연봉 백분위·설립·투자·퇴사율·복지) + 채용 활발도.

    태그 체계가 둘이라 둘 다 받는다 (2026-11-03 확인):
      /api/chaos/companies/v2/{id}  최신 체계 — 연봉 백분위·투자·복지 (값이 더 최근)
      /api/v4/companies/{id}        구 체계 — 퇴사율 등 신 체계에 없는 항목
    """
    s = _session()
    new = _get_json(s, f"{BASE}/api/chaos/companies/v2/{company_id}")["company"]
    try:
        old = _get_json(s, f"{BASE}/api/v4/companies/{company_id}")["company"]
    except Exception:  # noqa: BLE001  구 체계는 보조 정보라 실패해도 진행
        old = {}
    tags = [t["title"] for t in new.get("company_tags") or [] if t.get("title")]
    for t in old.get("company_tags") or []:
        if t.get("title") and t["title"] not in tags:
            tags.append(t["title"])
    stats = new.get("application_response_stats") or {}
    return {
        "name": new.get("name"),
        "founded_year": new.get("founded_year"),
        "industry": new.get("industry_name"),
        "tags": tags,
        "open_positions": new.get("confirmed_position_count"),
        "response_rate": stats.get("avg_rate"),
        # 원티드는 재무·사원수를 안 줌 → collect.refresh_companies 가 사람인에서 찾아 채움
        "employees": None, "revenue_eok": None, "op_income_eok": None, "revenue_growth": None,
        "company_type": None,
    }
