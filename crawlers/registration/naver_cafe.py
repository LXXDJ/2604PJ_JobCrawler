"""네이버 카페 자동 등록.

흐름:
  1. cafe URL → cafe slug + cafeId 추출
  2. 카페 메뉴 목록 fetch
  3. 직접 채용 게시판으로 보이는 메뉴 (구인공고/채용공고/모집/일자리/취업행사/구인구직)
     필터
  4. 각 메뉴에서 1페이지 fetch 검증 (≥3 게시글) — 빈 게시판 제외
  5. 각 메뉴를 별도 source 로 등록
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from ..fetchers.naver_cafe import fetch_articles_page, fetch_menus
from ..fetchers.static import fetch as fetch_static
from ..infra.sites_repo import upsert_site


# 직접 채용/구직 게시판으로 인정할 메뉴명 패턴 (Q&A·후기·자유게시판·행사 제외)
JOB_MENU_RE = re.compile(
    r"구인공고|채용공고|모집공고|구인구직|"
    r"구인정보|채용정보|해외구인|해외채용",
    re.I,
)
# 명백히 채용 게시판 아닌 키워드 (행사/대전/박람회는 단순 안내라 제외)
EXCLUDE_RE = re.compile(
    r"Q\s*&\s*A|FAQ|후기|자유|수다|방명록|공지|뉴스|정보$|소개|체크|영상|"
    r"비자정보|국가정보|등록안내|영사관|무역관|지원금|장려금|"
    r"행사|박람회|일자리대전|페어|fair|설명회",
    re.I,
)
MIN_VALID_ARTICLES = 1


@dataclass
class CafeRegisterReport:
    home_url: str
    site_id: str
    final_status: str = "pending"
    cafe_id: Optional[str] = None
    cafe_slug: Optional[str] = None
    cafe_name: Optional[str] = None
    candidate_menus: list[dict] = field(default_factory=list)
    sources: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def is_naver_cafe_url(url: str) -> bool:
    p = urlparse(url)
    return p.netloc.lower() in ("cafe.naver.com", "m.cafe.naver.com")


def parse_cafe_slug(url: str) -> Optional[str]:
    p = urlparse(url)
    parts = [s for s in p.path.split("/") if s]
    if not parts:
        return None
    # /f-e/cafes/{id}/menus/{m} 패턴이면 slug 없음
    if parts[0] == "f-e":
        return None
    return parts[0]


def _resolve_cafe_id_and_name(slug: str) -> tuple[Optional[str], Optional[str]]:
    """홈 HTML 에서 cafeId, cafe name 추출."""
    r = fetch_static(f"https://cafe.naver.com/{slug}", timeout=15)
    if not r.ok:
        return None, None
    html = r.text or ""
    m = re.search(r'clubid["\s:=]+(\d+)', html)
    cafe_id = m.group(1) if m else None
    # title 의 ` : 네이버 카페` 제거
    name = None
    mt = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
    if mt:
        t = re.sub(r"\s+", " ", mt.group(1).strip())
        t = re.sub(r"\s*[:：]\s*네이버\s*카페\s*$", "", t)
        name = t[:80] or None
    return cafe_id, name


def _filter_job_menus(menus: list[dict]) -> list[dict]:
    out = []
    for m in menus:
        if m.get("menuType") != "B":  # 'B' = 일반 게시판
            continue
        name = (m.get("name") or "").strip()
        if not name:
            continue
        if EXCLUDE_RE.search(name):
            continue
        if not JOB_MENU_RE.search(name):
            continue
        out.append(m)
    return out


def _build_source(cafe_id: str, slug: str, menu: dict, list_rows: int) -> dict:
    mid = menu["menuId"]
    return {
        "url": f"https://cafe.naver.com/f-e/cafes/{cafe_id}/menus/{mid}",
        "label": "full",
        "list_rows": list_rows,
        "subject_link_ratio": 1.0,
        "container_signature": f"naver_cafe.menu#{mid}",
        "fetcher": "naver_cafe",
        "cafe_id": str(cafe_id),
        "cafe_slug": slug,
        "menu_id": str(mid),
        "menu_name": menu.get("name"),
    }


def register_naver_cafe(
    home_url: str,
    *,
    name: Optional[str] = None,
    dry_run: bool = False,
) -> CafeRegisterReport:
    slug = parse_cafe_slug(home_url)
    if not slug:
        rep = CafeRegisterReport(home_url=home_url, site_id="")
        rep.notes.append("cafe slug not parseable from URL")
        return rep

    rep = CafeRegisterReport(home_url=home_url, site_id=slug, cafe_slug=slug)

    cafe_id, cafe_name = _resolve_cafe_id_and_name(slug)
    if not cafe_id:
        rep.final_status = "dead"
        rep.notes.append("cafe_id resolve failed (home fetch failed?)")
        if not dry_run:
            upsert_site(slug, home_url, name=name or cafe_name,
                        status="dead", status_reason="cafe_id_not_found",
                        sources=[])
        return rep
    rep.cafe_id = cafe_id
    rep.cafe_name = cafe_name

    menus, err = fetch_menus(cafe_id)
    if err or not menus:
        rep.final_status = "pending"
        rep.notes.append(f"menus api fail: {err or 'empty'}")
        if not dry_run:
            upsert_site(slug, home_url, name=name or cafe_name,
                        status="pending", status_reason="menus_api_failed",
                        sources=[])
        return rep

    candidates = _filter_job_menus(menus)
    rep.candidate_menus = [
        {"menuId": m["menuId"], "name": m["name"]} for m in candidates
    ]
    if not candidates:
        rep.final_status = "pending"
        rep.notes.append("no job-related menus matched (regex)")
        if not dry_run:
            upsert_site(slug, home_url, name=name or cafe_name,
                        status="pending", status_reason="no_job_menus",
                        sources=[])
        return rep

    sources: list[dict] = []
    for menu in candidates:
        items, perr = fetch_articles_page(cafe_id, menu["menuId"], page=1)
        if perr:
            rep.notes.append(f"menu {menu['menuId']} fetch fail: {perr}")
            continue
        if len(items) < MIN_VALID_ARTICLES:
            rep.notes.append(f"menu {menu['menuId']} ({menu['name']}) empty")
            continue
        sources.append(_build_source(cafe_id, slug, menu, list_rows=len(items)))

    rep.sources = sources
    if not sources:
        rep.final_status = "pending"
        rep.notes.append("all candidate menus empty")
        if not dry_run:
            upsert_site(slug, home_url, name=name or cafe_name,
                        status="pending", status_reason="all_menus_empty",
                        sources=[])
        return rep

    rep.final_status = "active"
    if not dry_run:
        upsert_site(
            slug, home_url, name=name or cafe_name,
            status="active", status_reason=None, sources=sources,
        )
    return rep
