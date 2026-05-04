"""WordPress 사이트 fetcher.

두 가지 모드:
  1) rest_categories — `/wp-json/wp/v2/posts?categories=N` REST API 페이지네이션.
                       표준 WP 글 (카테고리별).
  2) kboard_sitemap — KBoard 게시판 플러그인의 sitemap shard 들에서 URL 추출.
                       `?kboard_content_redirect=N` 형식. WP REST API 가 못 잡는
                       게시판 글 별도 시스템.

source 스키마 예:
  {"fetcher":"wordpress","mode":"rest_categories","base_url":"https://www.gwork.kr","category_id":32}
  {"fetcher":"wordpress","mode":"kboard_sitemap","base_url":"https://www.gwork.kr","sitemaps":["https://www.gwork.kr/kboard-sitemap1.xml", ...]}
"""
from __future__ import annotations

import html
import json
import re
from dataclasses import dataclass, field
from typing import Optional

from ..extractors.list_extractor import ExtractedRow
from .static import fetch as fetch_static


REST_PAGE_SIZE = 100
REST_MAX_PAGES = 50  # 안전 상한 (5000건)


@dataclass
class WPCrawlResult:
    rows: list[ExtractedRow] = field(default_factory=list)
    pages_crawled: int = 0
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _strip_html(s: str) -> str:
    return re.sub(r"<[^>]+>", "", html.unescape(s or "")).strip()


def crawl_rest_categories(
    base_url: str,
    category_id: int | str,
    *,
    already_seen_ids: Optional[set[str]] = None,
    max_pages: int = REST_MAX_PAGES,
    progress_cb=None,
) -> WPCrawlResult:
    """`/wp-json/wp/v2/posts?categories=N` 페이지네이션."""
    log = progress_cb or (lambda _msg: None)
    seen = set(already_seen_ids or ())
    result = WPCrawlResult()
    seen_ids_in_run: set[str] = set()
    base_url = base_url.rstrip("/")

    for page in range(1, max_pages + 1):
        url = (f"{base_url}/wp-json/wp/v2/posts"
               f"?categories={category_id}&per_page={REST_PAGE_SIZE}&page={page}"
               f"&_fields=id,title,link,date")
        log(f"    page {page} fetch (wp rest cat={category_id})...")
        r = fetch_static(url, timeout=20)
        if not r.ok:
            if page == 1:
                result.error = r.error or f"HTTP {r.status}"
            break
        try:
            data = json.loads(r.text or "[]")
        except Exception as e:  # noqa: BLE001
            if page == 1:
                result.error = f"json parse fail: {e}"
            break
        if not isinstance(data, list) or not data:
            break

        new_count = 0
        for item in data:
            pid = str(item.get("id") or "")
            link = item.get("link") or ""
            if not pid or not link:
                continue
            if pid in seen_ids_in_run:
                continue
            seen_ids_in_run.add(pid)
            if pid in seen:
                continue
            title = item.get("title")
            if isinstance(title, dict):
                title = title.get("rendered", "")
            title = _strip_html(title)[:200]
            if not title:
                continue
            result.rows.append(ExtractedRow(detail_url=link, title=title))
            new_count += 1

        result.pages_crawled = page
        log(f"    page {page}: items={len(data)} new={new_count}")
        if len(data) < REST_PAGE_SIZE:
            break

    return result


def crawl_kboard_sitemap(
    base_url: str,
    sitemaps: list[str],
    *,
    already_seen_ids: Optional[set[str]] = None,
    progress_cb=None,
) -> WPCrawlResult:
    """KBoard sitemap shard 들에서 `?kboard_content_redirect=N` URL 수집.
    각 URL 의 title 은 detail_crawler 가 detail fetch 시 추출 (여기선 ID 만 확보).
    """
    log = progress_cb or (lambda _msg: None)
    seen = set(already_seen_ids or ())
    result = WPCrawlResult()
    base_url = base_url.rstrip("/")

    for i, sm_url in enumerate(sitemaps, 1):
        log(f"    sitemap {i}/{len(sitemaps)} fetch ({sm_url})...")
        r = fetch_static(sm_url, timeout=20)
        if not r.ok:
            log(f"    sitemap {i} fail: {r.error or r.status}")
            if i == 1 and not result.rows:
                result.error = r.error or f"HTTP {r.status}"
            continue
        locs = re.findall(r"<loc>([^<]+)</loc>", r.text or "")
        new_count = 0
        for u in locs:
            m = re.search(r"kboard_content_redirect=(\d+)", u)
            if not m:
                continue
            kid = m.group(1)
            if kid in seen:
                continue
            # title 은 detail fetch 시 결정 — 여기선 placeholder
            result.rows.append(ExtractedRow(detail_url=u, title=f"kboard #{kid}"))
            new_count += 1
        result.pages_crawled = i
        log(f"    sitemap {i}: locs={len(locs)} new={new_count}")

    return result
