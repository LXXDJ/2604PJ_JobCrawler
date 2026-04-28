"""상세 URL → external_id 추출.

우선순위:
  1. URL path 의 마지막 숫자 segment    (/view/12345 → "12345")
  2. URL query 의 ID 류 파라미터 값      (?seq=12345, ?idx=999 → "12345")
  3. URL path 의 마지막 segment (숫자 아니어도)
  4. fallback: 정규화된 URL 자체

site 내에서 유일하면 됨 (UNIQUE(site_id, external_id)).
"""
from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlparse, urlunparse


_ID_PARAM_NAMES = {
    "id", "seq", "idx", "no", "num", "key", "code",
    "post_id", "postid", "board_seq", "boardseq", "article_no",
    "recruit_id", "recruitid", "job_id", "jobid",
}

# 페이지네이션 / 정렬 / 필터 / CMS 네비게이션 류 — ID 로 오해하면 안 됨
_NON_ID_PARAMS = {
    "page", "pagenum", "pageno", "pagesize", "currentpage", "p",
    "size", "limit", "offset", "start", "rows", "perpage",
    "sort", "order", "orderby", "dir", "direction",
    "method", "action", "view", "type", "tab", "lang", "locale",
    "search", "q", "query", "keyword",
    "year", "month", "day", "date",
    # CMS 네비게이션 / 카테고리 / 메뉴 ID — 게시물 detail ID 아님
    "menuid", "menu_id", "categoryid", "category_id", "cateid", "cate_id",
    "tabid", "tab_id", "mid", "nid", "parentid", "parent_id",
    "groupid", "group_id", "roleid", "themeid", "siteid", "site_id",
    "depth", "lvl", "level",
    # worldjob 류 추가 필터
    "dobtype", "dobType",
}

# `xxxSeq`, `xxxId`, `xxxNo` 등 suffix 패턴 ID
_ID_SUFFIX_RE = re.compile(r"(seq|id|no|idx|num|key|code|pk)$", re.I)

_NUMERIC = re.compile(r"^\d+$")


def _normalize_url(url: str) -> str:
    p = urlparse(url)
    # fragment 제거, trailing slash 통일은 보수적으로 그대로 둠 (사이트마다 다름)
    return urlunparse(p._replace(fragment=""))


def extract_external_id(url: str) -> str:
    p = urlparse(url)
    segments = [s for s in p.path.split("/") if s]

    # 1. path 마지막 숫자
    for seg in reversed(segments):
        if _NUMERIC.match(seg):
            return seg

    # 2. query 의 ID 류 파라미터 (정확 매칭 우선)
    qs = list(parse_qsl(p.query, keep_blank_values=False))
    for name, val in qs:
        if name.lower() in _ID_PARAM_NAMES and val:
            return val

    # 2.5. ID suffix 패턴 (recruitSeq, articleId, boardNo, ...)
    #      pagination/필터 키는 제외.
    for name, val in qs:
        low = name.lower()
        if low in _NON_ID_PARAMS:
            continue
        if _ID_SUFFIX_RE.search(low) and val:
            return val

    # 2.6. 그래도 없으면 숫자값 중 pagination 키 제외하고 첫 번째
    for name, val in qs:
        if name.lower() in _NON_ID_PARAMS:
            continue
        if val and _NUMERIC.match(val):
            return val

    # 3. path 마지막 segment (영숫자)
    if segments:
        return segments[-1]

    # 4. fallback: URL 자체
    return _normalize_url(url)
