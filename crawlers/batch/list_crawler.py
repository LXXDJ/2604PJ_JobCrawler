"""source URL → 페이지네이션 따라가며 모든 행 수집.

전략:
  1. page 1 fetch → 모든 컨테이너 후보의 row 합쳐서 detail_url 수집
  2. 가장 빈도 높은 detail URL 의 path-prefix 를 "정답 패턴" 으로 학습
     (예: https://job.example.com/recruit/view/ )
  3. page 1 의 row 중 prefix 매칭만 채택
  4. page 2+ : 같은 prefix 매칭된 row 만 채택, 새 row 0개면 break
  5. fetch 실패 / MAX_PAGES / 새 row 0개 → 종료

이렇게 하면:
  - 같은 페이지에 sticky 박스 + 본문 list 가 둘 다 있어도 합쳐 수집
  - 페이지가 끝난 후 카테고리 메뉴 같은 잡음은 자동 무시 (prefix 불일치)
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from ..extractors.list_extractor import ExtractedRow, MIN_ROWS, extract_list_multi, ListExtraction
from ..fetchers.static import fetch as fetch_static


PAGINATION_PARAMS = ["pageIndex", "currentPage", "page", "pageNum", "pageNo",
                     "cpage", "startPage", "p"]
MAX_PAGES = 200                   # 안전장치 (페이지 끝나면 자동 break)
NEW_ROWS_BREAK_THRESHOLD = 0.20   # 새 row 비율이 이 미만이면 페이지 끝으로 간주
CONSECUTIVE_LOW_BREAK = 2         # 연속 N 페이지 새 row 거의 없으면 break


@dataclass
class CrawlListResult:
    source_url: str
    rows: list[ExtractedRow] = field(default_factory=list)
    pages_crawled: int = 0
    detail_url_prefix: Optional[str] = None
    pagination_param: Optional[str] = None
    container_signature: Optional[str] = None  # page 1 에서 lock 한 시그니처
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _set_query_param(url: str, name: str, value: str) -> str:
    p = urlparse(url)
    qs = dict(parse_qsl(p.query, keep_blank_values=True))
    qs[name] = value
    return urlunparse(p._replace(query=urlencode(qs)))


def _detect_existing_page_param(url: str) -> Optional[str]:
    qs = dict(parse_qsl(urlparse(url).query, keep_blank_values=True))
    for name in PAGINATION_PARAMS:
        if name in qs:
            return name
    return None


def _detect_page_param_from_html(html: str, base_url: str) -> Optional[str]:
    """page 1 의 anchor href 에서 사용중인 pagination param 추론.

    예: 페이지 번호 anchor 가 `?method=recruitList&currentPage=2` 형태면
        'currentPage' 반환. (캐시: 가장 자주 등장하는 PAGINATION_PARAMS 후보)
    """
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")
    base_path = urlparse(base_url).path
    counts: dict[str, int] = {}
    for a in soup.find_all("a", href=True):
        href = a["href"]
        # javascript pagination: javascript:fn_search('2') 등 → 못 잡지만 querystring 만 노린다
        abs_url = urljoin(base_url, href)
        p = urlparse(abs_url)
        if p.path != base_path:
            continue
        qs = dict(parse_qsl(p.query, keep_blank_values=False))
        for name in PAGINATION_PARAMS:
            if name in qs and qs[name].isdigit():
                counts[name] = counts.get(name, 0) + 1
    if not counts:
        return None
    # 가장 많이 등장한 후보
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _detail_prefix(url: str) -> str:
    """detail URL → path 의 마지막 segment 만 제외한 prefix.

    예: https://job.career.co.kr/recruit/view/21850667
        → https://job.career.co.kr/recruit/view/
    """
    p = urlparse(url)
    segments = p.path.split("/")
    if len(segments) <= 1:
        new_path = "/"
    else:
        new_path = "/".join(segments[:-1]) + "/"
    return f"{p.scheme}://{p.netloc}{new_path}"


def _learn_prefix(rows: list[ExtractedRow]) -> Optional[str]:
    """row URL 들의 prefix 빈도 → 가장 흔한 prefix 채택."""
    prefixes = [_detail_prefix(r.detail_url) for r in rows if r.detail_url]
    if not prefixes:
        return None
    common, count = Counter(prefixes).most_common(1)[0]
    # 최소 2개 이상 같은 prefix 여야 의미 있음
    return common if count >= 2 else prefixes[0]


def _filter_by_prefix(rows: list[ExtractedRow], prefix: str) -> list[ExtractedRow]:
    return [r for r in rows if r.detail_url and r.detail_url.startswith(prefix)]


def _normalize_sig(sig: str) -> str:
    """'div.gtp#32' → 'div.gtp' (row 수 부분 제거)."""
    return sig.split("#", 1)[0]


def _pick_best_by_prefix(
    multi_candidates: list[ListExtraction], prefix: str
) -> Optional[ListExtraction]:
    """prefix 매칭 row 가 가장 많은 컨테이너 1개 채택."""
    best: Optional[ListExtraction] = None
    best_n = 0
    for ext in multi_candidates:
        matched = _filter_by_prefix(ext.rows, prefix)
        if len(matched) > best_n:
            best_n = len(matched)
            best = ext
    return best


def crawl_list(
    source_url: str,
    *,
    max_pages: int = MAX_PAGES,
    fetcher: str = "static",
    already_seen_ids: set[str] | None = None,
    id_extractor=None,
    progress_cb=None,
) -> CrawlListResult:
    """페이지네이션 따라가며 신규 row 만 수집 (증분).

    - already_seen_ids: 이미 DB 에 있는 external_id set.
                        한 페이지에서 already_seen 만 보이면 break (새 글 없음 → 종료).
    - id_extractor: detail_url → external_id 함수 (보통 extract_external_id).
                     없으면 detail_url 자체를 ID 로 사용.
    """
    if fetcher == "dynamic":
        from ..fetchers.dynamic import fetch as _fetch
    else:
        _fetch = fetch_static

    seen_ids = set(already_seen_ids or ())
    def _id_of(detail_url: str) -> str:
        return id_extractor(detail_url) if id_extractor else detail_url
    log = progress_cb or (lambda _msg: None)

    result = CrawlListResult(source_url=source_url)

    # ---- page 1
    log(f"    page 1 fetch ({fetcher})...")
    r = _fetch(source_url)
    if not r.ok:
        result.error = r.error or f"HTTP {r.status}"
        return result

    multi = extract_list_multi(r.text, r.final_url)
    if not multi.candidates:
        result.error = "no list container on page 1"
        return result

    # prefix 는 모든 후보에서 학습 (row 가 많은 쪽이 진짜 list)
    all_rows_p1 = multi.all_rows
    prefix = _learn_prefix(all_rows_p1)
    if not prefix:
        result.error = "could not learn detail prefix"
        return result
    result.detail_url_prefix = prefix

    # prefix 매칭 row 가 가장 많은 컨테이너 1개를 lock
    best = _pick_best_by_prefix(multi.candidates, prefix)
    if best is None:
        result.error = "page 1 has no container matching prefix"
        return result
    locked_sig = _normalize_sig(best.container_signature)
    result.container_signature = locked_sig

    seen_urls: set[str] = set()
    page1_new = 0
    page1_total = 0
    for row in _filter_by_prefix(best.rows, prefix):
        if row.detail_url in seen_urls:
            continue
        seen_urls.add(row.detail_url)
        page1_total += 1
        if _id_of(row.detail_url) in seen_ids:
            continue  # 이미 DB 에 있는 공고 → skip
        result.rows.append(row)
        page1_new += 1
    result.pages_crawled = 1

    if page1_total == 0:
        result.error = "page 1 rows did not match learned prefix"
        return result

    log(f"    page 1: total={page1_total} new={page1_new}")
    # 페이지 1 에 새 ID 가 0 이면 더 갈 필요 없음 (사이트 정렬이 최신순이라는 가정)
    if seen_ids and page1_new == 0:
        return result

    # ---- pagination param
    detected = (
        _detect_existing_page_param(source_url)
        or _detect_page_param_from_html(r.text, r.final_url)
    )
    # 자동감지 실패 시 — page 2 시도해서 실제로 다른 결과 나오는 candidate param 학습.
    # (worldjob 처럼 anchor 가 javascript: 라 query 추출이 안 되는 사이트 대응)
    if not detected:
        candidates = list(PAGINATION_PARAMS)
        learned = None
        for cand in candidates:
            tu = _set_query_param(source_url, cand, "2")
            log(f"    [learn page param] try {cand}={tu[len(source_url):]}")
            tr = _fetch(tu)
            if not tr.ok:
                continue
            tmulti = extract_list_multi(tr.text, tr.final_url)
            same_sig_ext = [e for e in tmulti.candidates
                            if _normalize_sig(e.container_signature) == locked_sig]
            if not same_sig_ext:
                continue
            tbest = max(same_sig_ext, key=lambda e: e.count)
            t_new = sum(1 for row in _filter_by_prefix(tbest.rows, prefix)
                        if row.detail_url and row.detail_url not in seen_urls)
            if t_new >= MIN_ROWS:
                learned = cand
                log(f"    [learn page param] LEARNED: {cand}  (new rows on page 2 = {t_new})")
                break
        page_param = learned or "page"
    else:
        page_param = detected
    result.pagination_param = page_param

    # ---- pages 2..N (같은 시그니처 + prefix 매칭만 채택)
    for page in range(2, max_pages + 1):
        page_url = _set_query_param(source_url, page_param, str(page))
        log(f"    page {page} fetch...")
        rp = _fetch(page_url)
        if not rp.ok:
            break

        multi_p = extract_list_multi(rp.text, rp.final_url)
        # 같은 시그니처 컨테이너만 사용
        same_sig = [e for e in multi_p.candidates
                    if _normalize_sig(e.container_signature) == locked_sig]
        if not same_sig:
            break

        page_ext = max(same_sig, key=lambda e: e.count)
        page_rows = _filter_by_prefix(page_ext.rows, prefix)
        if not page_rows:
            break

        page_new = 0
        page_total = 0
        for row in page_rows:
            if row.detail_url in seen_urls:
                continue
            seen_urls.add(row.detail_url)
            page_total += 1
            if _id_of(row.detail_url) in seen_ids:
                continue
            result.rows.append(row)
            page_new += 1

        result.pages_crawled = page
        log(f"    page {page}: total={page_total} new={page_new}")

        if page_total == 0:
            # 같은 row 가 이전 페이지와 완전히 동일 → 페이지네이션 끝
            log(f"    break: page {page} 모든 URL 이 이전 페이지와 동일")
            break
        if seen_ids and page_new == 0:
            # 모든 row 가 already-seen → 더 가도 새 글 없음 (증분 종료)
            log(f"    break: page {page} 모두 already_seen (증분 종료)")
            break

    return result
