"""네이버 카페 게시글 목록 fetcher.

네이버 카페는 정적 HTML 이 아닌 JSON API 를 통해 게시글을 노출.
비로그인 카페면 적절한 헤더만 있으면 토큰 없이 호출 가능.

사용:
  source 의 fetcher='naver_cafe' + cafe_id + menu_id 로 동작.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ..extractors.list_extractor import ExtractedRow
from .static import fetch as fetch_static


MAX_PAGES = 200
PAGE_SIZE = 50  # 카페 API 가 50 까지 허용


def _headers(cafe_id: str | int, menu_id: str | int) -> dict:
    return {
        "Referer": f"https://cafe.naver.com/f-e/cafes/{cafe_id}/menus/{menu_id}",
        "Origin": "https://cafe.naver.com",
        "X-Cafe-Product": "pc",
        "Accept": "application/json",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ),
    }


def _detail_url(cafe_id: str | int, article_id: str | int) -> str:
    return f"https://cafe.naver.com/f-e/cafes/{cafe_id}/articles/{article_id}"


@dataclass
class CafeCrawlResult:
    rows: list[ExtractedRow] = field(default_factory=list)
    pages_crawled: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fetch_menus(cafe_id: str | int) -> tuple[list[dict], Optional[str]]:
    """카페 메뉴 목록 조회. (menus, error)"""
    url = f"https://apis.naver.com/cafe-web/cafe-cafemain-api/v1.0/cafes/{cafe_id}/menus"
    r = fetch_static(url, timeout=15, headers=_headers(cafe_id, 0))
    if not r.ok:
        return [], r.error or f"HTTP {r.status}"
    try:
        data = json.loads(r.text or "{}")
    except Exception as e:  # noqa: BLE001
        return [], f"json parse fail: {e}"
    return data.get("result", {}).get("menus", []) or [], None


def fetch_articles_page(
    cafe_id: str | int,
    menu_id: str | int,
    page: int,
    *,
    page_size: int = PAGE_SIZE,
) -> tuple[list[dict], Optional[str]]:
    """1페이지 게시글 fetch. (items, error). items 는 articleList 의 'item' 풀어낸 리스트."""
    url = (
        f"https://apis.naver.com/cafe-web/cafe-boardlist-api/v1/cafes/{cafe_id}"
        f"/menus/{menu_id}/articles?page={page}&pageSize={page_size}"
        f"&sortBy=TIME&viewType=L"
    )
    r = fetch_static(url, timeout=15, headers=_headers(cafe_id, menu_id))
    if not r.ok:
        return [], r.error or f"HTTP {r.status}"
    try:
        data = json.loads(r.text or "{}")
    except Exception as e:  # noqa: BLE001
        return [], f"json parse fail: {e}"
    raw = data.get("result", {}).get("articleList", []) or []
    # articleList: [{"type": "ARTICLE"|"NOTICE"|..., "item": {...}}, ...]
    items = []
    for entry in raw:
        if entry.get("type") not in ("ARTICLE", "NOTICE"):
            continue
        it = entry.get("item")
        if isinstance(it, dict):
            items.append(it)
    return items, None


def crawl_cafe(
    cafe_id: str | int,
    menu_id: str | int,
    *,
    already_seen_ids: Optional[set[str]] = None,
    max_pages: int = MAX_PAGES,
    progress_cb=None,
) -> CafeCrawlResult:
    log = progress_cb or (lambda _msg: None)
    seen = set(already_seen_ids or ())
    result = CafeCrawlResult()
    seen_ids_in_run: set[str] = set()  # 페이지 간 중복 (게시판 reorder 방지)

    for page in range(1, max_pages + 1):
        log(f"    page {page} fetch (naver_cafe menu={menu_id})...")
        items, err = fetch_articles_page(cafe_id, menu_id, page)
        if err:
            if page == 1:
                result.error = err
            break
        if not items:
            break

        new_count = 0
        seen_count = 0
        for it in items:
            aid = str(it.get("articleId") or "")
            if not aid:
                continue
            if aid in seen_ids_in_run:
                continue
            seen_ids_in_run.add(aid)
            title = (it.get("subject") or "").strip()
            if not title:
                continue
            row = ExtractedRow(detail_url=_detail_url(cafe_id, aid), title=title[:200])
            result.rows.append(row)
            new_count += 1
            if aid in seen:
                seen_count += 1

        result.pages_crawled = page
        log(f"    page {page}: total={len(items)} new={new_count} already_seen={seen_count}")

        # 모두 already_seen 이고 페이지가 가득 찼으면 더 안 나옴 (증분 종료)
        if new_count > 0 and seen_count == new_count and len(items) >= PAGE_SIZE:
            log(f"    break: page {page} 모든 글이 이미 봤음")
            break
        # 페이지가 안 차면 마지막
        if len(items) < PAGE_SIZE:
            break

    return result
