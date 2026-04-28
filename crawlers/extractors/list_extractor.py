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


def _abs(base: str, href: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    if href.startswith(("mailto:", "tel:")) or href == "#":
        return None
    if href.startswith("javascript:"):
        # 예: javascript:goView1('E20260428002','1','1','1') →
        #     synthetic URL = base?_jsfn=goView1&_jsid=E20260428002
        # row 마다 첫 인자가 unique 하면 list 로 인정 가능.
        # query 로 박는 이유: external_id 가 _jsid (id-suffix) 로 추출 가능.
        m = _JS_ID_RE.match(href)
        if not m:
            return None
        fn, arg = m.group(1), m.group(2)
        from urllib.parse import parse_qsl, urlencode
        bp = urlparse(base)
        qs = dict(parse_qsl(bp.query, keep_blank_values=True))
        qs["_jsfn"] = fn
        qs["_jsid"] = arg
        return urlunparse(bp._replace(query=urlencode(qs)))
    abs_url = urljoin(base, href)
    p = urlparse(abs_url)
    if p.scheme not in ("http", "https"):
        return None
    return abs_url


def _extract_subject(row: Tag, base_url: str) -> Optional[ExtractedRow]:
    """행 안에서 첫 번째 '제목 링크' 후보를 찾아 반환."""
    best: Optional[ExtractedRow] = None
    best_len = 0
    for a in row.find_all("a", href=True):
        url = _abs(base_url, a["href"])
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


def extract_list_multi(html: str, base_url: str) -> ListExtractionMulti:
    """모든 list 후보 컨테이너를 반환 (sticky + 본문 동시 케이스 대응)."""
    soup = BeautifulSoup(html, "html.parser")
    raw: list[tuple[str, list[Tag]]] = []
    raw += _candidate_table(soup)
    raw += _candidate_ul(soup)
    raw += _candidate_repeating_div(soup, base_url)

    out: list[ListExtraction] = []
    for sig, rows in raw:
        extracted = [r for r in (_extract_subject(row, base_url) for row in rows) if r]
        if not extracted:
            continue
        out.append(ListExtraction(container_signature=sig, rows=extracted))
    return ListExtractionMulti(candidates=out)


def extract_list(html: str, base_url: str) -> ListExtraction:
    """기존 호환: 가장 큰 후보 1개만 반환."""
    return extract_list_multi(html, base_url).best
