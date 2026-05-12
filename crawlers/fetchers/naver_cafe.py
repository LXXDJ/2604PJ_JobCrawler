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

from bs4 import BeautifulSoup

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
            if aid in seen:
                # 이미 DB 에 있는 글 — yield 안 함 (중복 적재 방지)
                seen_count += 1
                continue
            title = (it.get("subject") or "").strip()
            if not title:
                continue
            row = ExtractedRow(detail_url=_detail_url(cafe_id, aid), title=title[:200])
            result.rows.append(row)
            new_count += 1

        result.pages_crawled = page
        log(f"    page {page}: total={len(items)} new={new_count} already_seen={seen_count}")

        # 새 글 0 이고 already_seen 이 있으면 — 이 페이지는 모두 기존 글, 증분 종료
        if new_count == 0 and seen_count > 0:
            log(f"    break: page {page} 신규 글 없음 (모두 기존 글)")
            break
        # 페이지가 안 차면 마지막
        if len(items) < PAGE_SIZE:
            break

    return result


# ============================================================================
# Article detail (공개 카페만 — 회원전용 카페는 401)
# ============================================================================

def fetch_article_detail(cafe_id: str | int, article_id: str | int, *, timeout: int = 15):
    """카페 게시글 detail JSON API → JobDetail 변환.

    공개 카페만 동작. 회원전용 카페는 401 반환 → JobDetail.error 에 명시.
    """
    # 순환 import 회피용 lazy import
    from ..batch.detail_crawler import (
        JobDetail, _absolutize_html, _extract_images,
        _extract_links_and_attachments, _extract_iframes_videos,
        _extract_tables, _extract_contacts, _strip_trailing_nav,
        BODY_MAX_CHARS, BODY_HTML_MAX_CHARS,
    )

    detail_url = _detail_url(cafe_id, article_id)
    api = (
        f"https://apis.naver.com/cafe-web/cafe-articleapi/v2.1/cafes/{cafe_id}"
        f"/articles/{article_id}?query=&useCafeId=true&requestFrom=A"
    )
    r = fetch_static(api, timeout=timeout, headers=_headers(cafe_id, 0))
    if not r.ok:
        return JobDetail(url=detail_url, error=r.error or f"HTTP {r.status}")
    try:
        data = json.loads(r.text or "{}")
    except Exception as e:  # noqa: BLE001
        return JobDetail(url=detail_url, error=f"json parse fail: {e}")

    res = data.get("result") if isinstance(data, dict) else {}
    if not isinstance(res, dict):
        return JobDetail(url=detail_url, error="malformed response")
    art = res.get("article")
    if not isinstance(art, dict):
        # 회원전용/비공개 — errorCode 0004 등
        err_code = res.get("errorCode") or "?"
        msg = res.get("reason") or res.get("message") or "no article"
        return JobDetail(url=detail_url, error=f"cafe_api {err_code}: {msg}")

    title = (art.get("subject") or "").strip()
    content_html = art.get("contentHtml") or art.get("content") or ""

    # contentHtml 의 [[[CONTENT-ELEMENT-N]]] placeholder 를 contentElements[N] 의
    # 실제 HTML 로 교체 (이미지/비디오 inline 렌더 위함)
    elements_raw = art.get("contentElements") or []
    if isinstance(elements_raw, list) and elements_raw:
        import re as _re
        from html import escape as _escape
        def _element_to_html(e: dict) -> str:
            t = e.get("type") if isinstance(e, dict) else None
            j = e.get("json") if isinstance(e, dict) else None
            if not isinstance(j, dict):
                return ""
            if t == "IMAGE":
                img = j.get("image") or {}
                u = img.get("url") or ""
                alt = _escape(img.get("fileName") or "")
                if u:
                    return f'<img src="{_escape(u)}" alt="{alt}" />'
            elif t in ("VIDEO", "MOVIE"):
                v = j.get("video") or j
                u = v.get("url") or v.get("playUrl") or ""
                if u:
                    return f'<video src="{_escape(u)}" controls></video>'
            elif t in ("FILE", "ATTACH"):
                f = j.get("file") or j
                u = f.get("url") or f.get("downloadUrl") or ""
                name = _escape(f.get("fileName") or f.get("name") or "첨부")
                if u:
                    return f'<a href="{_escape(u)}">{name} 다운로드</a>'
            elif t in ("OGLINK", "LINK"):
                l = j.get("link") or j
                u = l.get("link") or l.get("url") or ""
                desc = _escape(l.get("title") or l.get("description") or u)
                if u:
                    return f'<a href="{_escape(u)}">{desc}</a>'
            return ""
        def _replace(m):
            idx = int(m.group(1))
            if 0 <= idx < len(elements_raw):
                return _element_to_html(elements_raw[idx])
            return ""
        content_html = _re.sub(r"\[\[\[CONTENT-ELEMENT-(\d+)\]\]\]",
                               _replace, content_html)

    soup = BeautifulSoup(content_html, "html.parser")
    _absolutize_html(soup, detail_url)
    body_text = " ".join(soup.get_text(" ", strip=True).split())[:BODY_MAX_CHARS]
    body_text = _strip_trailing_nav(body_text)
    body_html = str(soup)[:BODY_HTML_MAX_CHARS]

    images = _extract_images(soup, detail_url)
    links, attachments = _extract_links_and_attachments(soup, detail_url)
    iframes, videos = _extract_iframes_videos(soup, detail_url)
    tables = _extract_tables(soup)
    emails, phones = _extract_contacts(body_text)

    # 카페 신형 에디터 (Smart Editor 3) — contentElements 안에 이미지/파일/비디오가
    # component 형태로 저장되며 contentHtml 에는 없을 수 있음.
    elements = art.get("contentElements") or []
    if isinstance(elements, list):
        seen_imgs = set(images)
        for e in elements:
            if not isinstance(e, dict):
                continue
            t = e.get("type")
            j = e.get("json") or {}
            if not isinstance(j, dict):
                continue
            if t == "IMAGE":
                img_obj = j.get("image") or {}
                u = img_obj.get("url")
                if u and u not in seen_imgs:
                    images.append(u)
                    seen_imgs.add(u)
            elif t in ("FILE", "ATTACH"):
                file_obj = j.get("file") or j
                u = file_obj.get("url") or file_obj.get("downloadUrl") or ""
                name = file_obj.get("fileName") or file_obj.get("name") or ""
                if u:
                    ext = name.rsplit(".", 1)[-1].lower()[:6] if "." in name else ""
                    existing = {x["url"] for x in attachments}
                    if u not in existing:
                        attachments.append({"url": u, "text": name, "ext": ext})
            elif t in ("VIDEO", "MOVIE"):
                v_obj = j.get("video") or j
                u = v_obj.get("url") or v_obj.get("playUrl") or ""
                if u and u not in videos:
                    videos.append(u)
            elif t in ("OGLINK", "LINK"):
                l_obj = j.get("link") or j
                u = l_obj.get("link") or l_obj.get("url") or ""
                desc = l_obj.get("title") or l_obj.get("description") or ""
                if u and not any(ln.get("url") == u for ln in links):
                    links.append({"url": u, "text": desc[:200]})

    # 카페 article 자체의 첨부 파일 (writeAttaches/attaches/attachFiles 등) 합치기
    for key in ("attaches", "attachFiles", "fileList"):
        items = art.get(key) or []
        if not isinstance(items, list):
            continue
        for a in items:
            if not isinstance(a, dict):
                continue
            url = a.get("url") or a.get("downloadUrl") or a.get("path") or ""
            name = a.get("name") or a.get("fileName") or ""
            if not url:
                continue
            ext = ""
            if "." in name:
                ext = name.rsplit(".", 1)[-1].lower()[:6]
            existing = {x["url"] for x in attachments}
            if url not in existing:
                attachments.append({"url": url, "text": name, "ext": ext})

    # 카페 메타 (작성자/조회수/댓글수/작성일)
    writer = (art.get("writerInfo") or {}) if isinstance(art.get("writerInfo"), dict) else {}
    meta = {
        "naver_cafe": True,
        "writer": writer.get("nickName") or writer.get("memberKey"),
        "writeDateTimestamp": art.get("writeDateTimestamp"),
        "readCount": art.get("readCount"),
        "commentCount": art.get("commentCount"),
        "likeCount": art.get("likeCount"),
    }

    return JobDetail(
        url=detail_url,
        title=title,
        raw_text_snippet=body_text,
        body_html=body_html,
        images=images,
        links=links,
        attachments=attachments,
        iframes=iframes,
        videos=videos,
        emails=emails,
        phones=phones,
        tables=tables,
        meta=meta,
        jsonld=[],
    )
