"""HTML 에서 공고 리스트(rows) 와 상세 링크를 뽑아내는 공용 로직.

validator/batch 공용. validator 는 "검증" 만 하고, batch 는 "수집" 함.
"""
from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup, Tag


MIN_ROWS = 2
# anchor 텍스트 최소 길이 — 1자/2자 회사명 (예: "DK", "디앤") 도 받기 위해 2.
# pagination/navigation 같은 1자 anchor ("1", "▶") 만 거르는 정도면 충분.
MIN_LINK_TEXT_LEN = 2


@dataclass
class ExtractedRow:
    detail_url: str
    title: str = ""
    # 행 전체 셀 텍스트 (제목 외 컬럼 — 분야/직종, 업종 등 — 까지 포함).
    # job_title_ratio 검증 시 title 보조로 사용 (회사명만 anchor 이고 직종은
    # 별도 td 인 EPS 형식 대응).
    row_text: str = ""

    def to_dict(self) -> dict:
        return {"detail_url": self.detail_url, "title": self.title}


@dataclass
class ListExtraction:
    container_signature: str = ""
    rows: list[ExtractedRow] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.rows)


@dataclass
class ListExtractionMulti:
    """페이지 전체에서 list 후보가 여러 개 있을 때 모두 반환."""
    candidates: list[ListExtraction] = field(default_factory=list)

    @property
    def best(self) -> ListExtraction:
        if not self.candidates:
            return ListExtraction()
        return max(self.candidates, key=lambda e: e.count)

    @property
    def all_rows(self) -> list[ExtractedRow]:
        seen: set[str] = set()
        merged: list[ExtractedRow] = []
        for ext in self.candidates:
            for row in ext.rows:
                if row.detail_url and row.detail_url not in seen:
                    seen.add(row.detail_url)
                    merged.append(row)
        return merged


_JS_ID_RE = re.compile(
    r"^javascript:\s*([A-Za-z_][\w$]*)\s*\(\s*['\"]([^'\"]+)['\"]"
)

# onclick 에서 fn('ID') 패턴 추출. JSP 정부사이트 흔한 pattern:
#   <a href="#" onclick="javascript:goView('35261');">
_ONCLICK_FN_RE = re.compile(
    r"(?:javascript:\s*)?([A-Za-z_][\w$]*)\s*\(\s*['\"]([^'\"]+)['\"]"
)


def _effective_href(a: Tag) -> str:
    """href 가 '#' 또는 비어있을 때 onclick 에서 fn('ID') 추출해 javascript: 형태로 변환.
    그 외 경우는 원래 href 반환."""
    href = (a.get("href") or "").strip()
    if href and href != "#" and not href.startswith("javascript:void"):
        return href
    onclick = (a.get("onclick") or "").strip()
    if onclick:
        m = _ONCLICK_FN_RE.search(onclick)
        if m:
            return f"javascript:{m.group(1)}('{m.group(2)}')"
    return href


_SORT_ONLY_PARAMS = {
    "sst", "sod", "sfl", "stx", "sca",       # gnuboard5 search/sort
    "page", "pageIndex", "pagenumber", "pageNum", "pageNo", "currentPage",
    "sort", "order", "orderby", "by", "dir", "asc", "desc",
    "sop", "search_field", "search_str", "scategory",
}
_ID_LIKE_PARAMS = {
    "wr_id", "idx", "no", "seq", "id", "post", "article", "content_id",
    "boardId", "post_id", "article_id", "uid", "view_no", "num",
}


def _is_sort_or_pagination_only(url: str) -> bool:
    """URL query 가 sort/pagination 파라미터들로만 구성되어 있고 id-like 가 없으며
    path 가 list 페이지처럼 보이면 True. 헤더의 정렬 link, 페이지네이션 link 등을
    detail row 로 오인하지 않기 위함.

    예외: path 의 마지막 segment 가 숫자(또는 ID 형태) 이면 detail URL 로 취급.
    예: /jobs/6196864?page=2 — path 끝이 ID 라 detail. query 의 page 무시.
    """
    from urllib.parse import urlparse, parse_qs
    p = urlparse(url)
    if not p.query:
        return False
    # path 의 마지막 segment 가 ID-like (숫자 또는 영숫자 ID) 이면 detail URL
    last_seg = p.path.rstrip("/").rsplit("/", 1)[-1]
    if last_seg and (last_seg.isdigit() or
                     (len(last_seg) >= 8 and any(c.isdigit() for c in last_seg)
                      and not last_seg.endswith((".jsp", ".php", ".do", ".html", ".htm", ".asp")))):
        return False
    qs = parse_qs(p.query, keep_blank_values=True)
    keys = set(qs.keys())
    # id-like 파라미터가 있으면 정상 detail URL
    if keys & _ID_LIKE_PARAMS:
        return False
    # 모든 키가 sort/pagination 류이면 거름
    return bool(keys) and keys.issubset(_SORT_ONLY_PARAMS | {"bo_table", "tab", "category"})


def _abs(base: str, href: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:")) or href == "#":
        return None
    if href.startswith("javascript:"):
        # 예: javascript:goView1('E20260428002','1','1','1') →
        #     synthetic URL = scheme://host/path?_jsfn=goView1&_jsid=E20260428002
        # row 마다 첫 인자가 unique 하면 list 로 인정 가능.
        # query 로 박는 이유: external_id 가 _jsid (id-suffix) 로 추출 가능.
        # base 의 다른 query 는 의도적으로 버림 — 페이지네이션 query (page/currentPage)
        # 가 섞이면 같은 ID 도 페이지마다 URL 이 달라져 dedupe 가 깨짐.
        # unrecruit.mofa.go.kr 처럼 'javascript:javascript:goView(...)' 중복 prefix
        # 가 들어간 케이스 — 모두 떼고 매치.
        while href.startswith("javascript:"):
            href = href[len("javascript:"):].lstrip()
        href = "javascript:" + href
        m = _JS_ID_RE.match(href)
        if not m:
            return None
        fn, arg = m.group(1), m.group(2)
        from urllib.parse import urlencode
        bp = urlparse(base)
        return urlunparse(bp._replace(
            query=urlencode({"_jsfn": fn, "_jsid": arg}),
            fragment="",
        ))
    abs_url = urljoin(base, href)
    p = urlparse(abs_url)
    if p.scheme not in ("http", "https"):
        return None
    if _is_sort_or_pagination_only(abs_url):
        return None
    return abs_url


def _extract_subject(row: Tag, base_url: str) -> Optional[ExtractedRow]:
    """단일 row 만 보고 anchor 채택 — fallback 용 (longest text)."""
    best: Optional[ExtractedRow] = None
    best_len = 0
    for a in row.find_all("a"):
        href = _effective_href(a)
        url = _abs(base_url, href)
        if not url:
            continue
        text = a.get_text(" ", strip=True)
        if len(text) < MIN_LINK_TEXT_LEN:
            continue
        if len(text) > best_len:
            best = ExtractedRow(detail_url=url, title=text[:200])
            best_len = len(text)
    if best is not None:
        best.row_text = row.get_text(" ", strip=True)[:400]
    return best


def _anchor_fingerprint(href: str) -> str:
    """anchor 그룹화 키. busiInfoPopup vs goView1 구분.
    같은 함수명/path 의 anchor 는 같은 fingerprint.
    """
    href = (href or "").strip()
    if href.startswith("javascript:"):
        m = re.match(r"javascript:\s*([A-Za-z_][\w$]*)", href)
        return f"js:{m.group(1)}" if m else "js:?"
    p = urlparse(href)
    return f"{p.scheme}://{p.netloc}{p.path}" if p.netloc else p.path


def _pick_subjects_for_container(
    rows: list[Tag], base_url: str, *,
    prefix_hint: Optional[str] = None,
) -> list[Optional[ExtractedRow]]:
    """컨테이너 내 row 들의 anchor 분포를 분석해서 row 별 detail anchor 채택.

    핵심: 같은 회사의 다른 공고가 row 마다 회사 popup anchor (`busiInfoPopup`)
    + 채용공고 anchor (`goView1`) 둘 다 갖는 worldjob 케이스 처리.
    회사 popup 은 컨테이너 전체에서 ID 가 회사 단위 (중복 多), 채용공고 anchor 는
    row 별 unique. 후자 우선 채택.

    prefix_hint: 이전 페이지에서 학습된 detail URL prefix. 있으면 prefix 매치
    anchor 우선 채택 (peoplenjob 처럼 row 안에 여러 anchor 있고 첫 anchor 가
    회사 link 인 경우 page 2+ 에서 break 되는 버그 fix).

    알고리즘:
      1. row 별 anchor 수집 (fingerprint, url, text)
      2. fingerprint 별 통계: 등장 row 수 + unique URL 수
      3. 채택 기준: row 의 절반 이상에 등장 + unique URL 비율 ≥ 90%
         (= row 마다 다른 ID, 진짜 detail anchor)
      4. 채택 fp 의 anchor 가 row 에 있으면 그것을, 없으면 longest text fallback.
    """
    n_rows = len(rows)
    # row 별 anchor 수집
    row_anchors: list[list[tuple[str, str, str]]] = []
    for row in rows:
        items: list[tuple[str, str, str]] = []
        for a in row.find_all("a"):
            href = _effective_href(a)
            url = _abs(base_url, href)
            if not url:
                continue
            text = a.get_text(" ", strip=True)
            if len(text) < MIN_LINK_TEXT_LEN:
                continue
            items.append((_anchor_fingerprint(href), url, text))
        row_anchors.append(items)

    # fingerprint 별 통계 — 각 fp 가 컨테이너 안에서 얼마나 unique 한지
    fp_stats: dict[str, dict] = {}
    for items in row_anchors:
        seen_fps: set[str] = set()
        for fp, url, _t in items:
            stats = fp_stats.setdefault(fp, {"urls": set(), "rows": 0})
            stats["urls"].add(url)
            if fp not in seen_fps:
                stats["rows"] += 1
                seen_fps.add(fp)

    def _fp_score(fp: str) -> float:
        """fp 의 detail-anchor 적합도. row 마다 unique URL 일수록 높음.
        예: goView1 (row 별 unique) → 1.0
            busiInfoPopup (회사 popup, 중복 多) → 0.6 정도
        """
        st = fp_stats.get(fp)
        if not st or st["rows"] == 0:
            return -1.0
        return len(st["urls"]) / st["rows"]

    out: list[Optional[ExtractedRow]] = []
    for ri, items in enumerate(row_anchors):
        # prefix_hint 가 있으면 prefix 매치 anchor 우선 (page 2+ 에서 정답 패턴
        # 알고 있을 때 noise anchor 잡지 않도록)
        if prefix_hint:
            for fp, url, text in items:
                if url.startswith(prefix_hint):
                    chosen = ExtractedRow(detail_url=url, title=text[:200])
                    chosen.row_text = rows[ri].get_text(" ", strip=True)[:400]
                    out.append(chosen)
                    break
            else:
                out.append(None)
            continue

        chosen: Optional[ExtractedRow] = None
        chosen_score = -1.0
        for fp, url, text in items:
            sc = _fp_score(fp)
            # 동점이면 longer text 우선
            if sc > chosen_score or (sc == chosen_score and chosen is not None and
                                     len(text) > len(chosen.title)):
                chosen = ExtractedRow(detail_url=url, title=text[:200])
                chosen_score = sc
        if chosen is not None:
            chosen.row_text = rows[ri].get_text(" ", strip=True)[:400]
        out.append(chosen)
    return out


def _children_signature(el: Tag) -> str:
    tag = el.name or ""
    classes = ".".join(sorted(el.get("class") or []))
    return f"{tag}.{classes}" if classes else tag


def _tag_signature(el: Tag) -> str:
    """tag + id + class 까지 포함한 안정적 시그니처."""
    parts = [el.name or ""]
    if el.get("id"):
        parts.append(f"#{el['id']}")
    classes = el.get("class") or []
    if classes:
        parts.append("." + ".".join(sorted(classes)))
    return "".join(parts)


_NAV_SEMANTIC_TAGS = {"nav", "aside", "header", "footer"}


def _is_inside_nav(el: Tag) -> bool:
    """el 의 부모 체인에 nav/aside/header/footer 가 있으면 True."""
    p = el.parent
    while p is not None and getattr(p, "name", None):
        if p.name in _NAV_SEMANTIC_TAGS:
            return True
        p = p.parent
    return False


def _candidate_table(soup: BeautifulSoup) -> list[tuple[str, list[Tag]]]:
    out = []
    for tbl in soup.find_all("table"):
        if _is_inside_nav(tbl):
            continue
        body = tbl.find("tbody") or tbl
        rows = [r for r in body.find_all("tr", recursive=False) if r.find("td")]
        if len(rows) >= MIN_ROWS:
            sig = _tag_signature(tbl) + f"#{len(rows)}"
            out.append((sig, rows))
    return out


def _candidate_ul(soup: BeautifulSoup) -> list[tuple[str, list[Tag]]]:
    out = []
    for ul in soup.find_all(["ul", "ol"]):
        if _is_inside_nav(ul):
            continue
        items = ul.find_all("li", recursive=False)
        if len(items) >= MIN_ROWS:
            sig = _tag_signature(ul) + f"#{len(items)}"
            out.append((sig, items))
    return out


def _candidate_repeating_div(soup: BeautifulSoup, base_url: str) -> list[tuple[str, list[Tag]]]:
    out = []
    for parent in soup.find_all(True):
        if _is_inside_nav(parent):
            continue
        children = [c for c in parent.find_all(recursive=False) if isinstance(c, Tag)]
        if len(children) < MIN_ROWS:
            continue
        sigs = [_children_signature(c) for c in children]
        common, count = Counter(sigs).most_common(1)[0]
        if count >= MIN_ROWS and common.startswith("div"):
            rows = [c for c in children if _children_signature(c) == common]
            n_subj = sum(1 for r in rows if _extract_subject(r, base_url))
            if n_subj >= max(MIN_ROWS, len(rows) // 2):
                # 부모 시그니처도 포함시켜 다른 위치의 같은 패턴과 구분
                parent_sig = _tag_signature(parent)
                out.append((f"{parent_sig}>{common}#{len(rows)}", rows))
    return out


def extract_list_multi(html: str, base_url: str, *,
                       prefix_hint: Optional[str] = None) -> ListExtractionMulti:
    """모든 list 후보 컨테이너를 반환 (sticky + 본문 동시 케이스 대응).

    prefix_hint: 페이지 2+ 에서 학습된 detail prefix 전달 시 그 prefix 매치 anchor 우선.
    """
    soup = BeautifulSoup(html, "html.parser")
    raw: list[tuple[str, list[Tag]]] = []
    raw += _candidate_table(soup)
    raw += _candidate_ul(soup)
    raw += _candidate_repeating_div(soup, base_url)

    out: list[ListExtraction] = []
    for sig, rows in raw:
        # 컨테이너 단위 분석 — row 별 unique anchor (busiInfoPopup 같은 회사 popup
        # 보다 goView1 같은 detail anchor 우선) 채택
        extracted = [r for r in _pick_subjects_for_container(rows, base_url, prefix_hint=prefix_hint) if r]
        if not extracted:
            continue
        out.append(ListExtraction(container_signature=sig, rows=extracted))
    return ListExtractionMulti(candidates=out)


def extract_list(html: str, base_url: str) -> ListExtraction:
    """기존 호환: 가장 큰 후보 1개만 반환."""
    return extract_list_multi(html, base_url).best
