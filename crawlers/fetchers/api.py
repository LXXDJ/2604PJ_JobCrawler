"""학습된 API schema 로 페이지네이션하며 모든 row 가져오기.

source 의 fetcher='api' + api_schema 로 동작.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional

from ..extractors.api_schema import ApiSchema, get_at_path
from ..extractors.list_extractor import ExtractedRow
from .static import fetch as fetch_static


MAX_PAGES = 200


@dataclass
class ApiCrawlResult:
    rows: list[ExtractedRow] = field(default_factory=list)
    pages_crawled: int = 0
    error: Optional[str] = None
    total_count: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _format_url(pattern: str, *, page: int, size: int) -> str:
    return pattern.replace("{page}", str(page)).replace("{size}", str(size))


def _row_from_entry(entry: dict, schema: ApiSchema) -> Optional[ExtractedRow]:
    eid = entry.get(schema.id_field)
    title = entry.get(schema.title_field)
    if not eid or not title:
        return None
    if schema.detail_url_template:
        detail_url = schema.detail_url_template.replace("{id}", str(eid))
    else:
        # fallback: id 만 노출 (DB 적재시 url 비면 안되므로 schema.base_url + id)
        detail_url = f"{schema.base_url}/{eid}"
    return ExtractedRow(detail_url=detail_url, title=str(title)[:200])


def crawl_api(
    schema: ApiSchema,
    *,
    already_seen_ids: Optional[set[str]] = None,
    max_pages: int = MAX_PAGES,
    use_proxy: bool = False,
) -> ApiCrawlResult:
    seen = set(already_seen_ids or ())
    result = ApiCrawlResult()

    # 프록시 풀 — anti-scraping API (슈퍼루키 등) 가 무프록시 차단 시 회전 사용.
    # 풀 전체 실패 시 무프록시 fallback 1회.
    from ..infra.config import PROXIES
    _proxy_pool = list(PROXIES) if use_proxy else []
    _proxy_idx = {"i": 0}

    def _next_proxy() -> Optional[str]:
        if not _proxy_pool:
            return None
        p = _proxy_pool[_proxy_idx["i"] % len(_proxy_pool)]
        _proxy_idx["i"] += 1
        return p

    def _do_fetch(u: str):
        if not (use_proxy and _proxy_pool):
            return fetch_static(u, headers={"Accept": "application/json"})
        last = None
        for _ in range(len(_proxy_pool)):
            proxy = _next_proxy()
            r = fetch_static(u, headers={"Accept": "application/json"}, proxy=proxy)
            if r.ok:
                return r
            last = r
        # 풀 전체 실패 → 무프록시 fallback
        r = fetch_static(u, headers={"Accept": "application/json"})
        return r if r.ok else (last or r)

    for page in range(1, max_pages + 1):
        url = _format_url(schema.api_url_pattern,
                          page=page, size=schema.page_size)
        r = _do_fetch(url)
        if not r.ok:
            if page == 1:
                result.error = r.error or f"HTTP {r.status}"
            break
        try:
            data = json.loads(r.text)
        except Exception as e:  # noqa: BLE001
            if page == 1:
                result.error = f"json parse fail: {e}"
            break

        rows_data = get_at_path(data, schema.list_path)
        if not isinstance(rows_data, list) or not rows_data:
            break

        # totalCount 있으면 캐시
        if result.total_count is None:
            for k in ("totalCount", "total", "count", "totalElements"):
                tot = data.get(k) if isinstance(data, dict) else None
                if tot is None and isinstance(data, dict):
                    inner = data.get("data")
                    if isinstance(inner, dict):
                        tot = inner.get(k)
                if tot is not None:
                    try:
                        result.total_count = int(tot)
                    except (ValueError, TypeError):
                        pass
                    break

        page_new = 0
        for entry in rows_data:
            row = _row_from_entry(entry, schema)
            if row is None:
                continue
            eid = entry.get(schema.id_field)
            if eid is not None and str(eid) in seen:
                continue
            result.rows.append(row)
            page_new += 1
            if eid is not None:
                seen.add(str(eid))

        result.pages_crawled = page

        # 새로운 row 0개 + 이미 본 ID set 이 있으면 (증분 모드) 종료
        if seen and page_new == 0:
            break
        # 응답 자체가 비면 끝
        if len(rows_data) < schema.page_size:
            break

    return result
