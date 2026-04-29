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
from ..fetchers.static import fetch as fetch_static, make_session as _make_session


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


def _detect_path_pagination_template(html: str, source_url: str) -> Optional[str]:
    """path-segment 페이지네이션 학습.

    page 1 의 anchor 들 중 텍스트가 숫자 (1~999) 인 anchor 의 href 분석.
    href 들 사이에서 변하는 부분이 페이지 번호인 path-segment 면 그 위치를 {N}
    placeholder 로. 학습 성공 시 URL template 반환 (e.g.
    `https://www.cambojob.com/jobs/jobs_list/page/{N}.htm`).

    cambojob 처럼 query 가 아닌 path 로 페이지네이션 하는 사이트 대응.
    """
    import re as _re
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin

    soup = BeautifulSoup(html, "html.parser")
    src_p = urlparse(source_url)

    # 텍스트가 숫자인 anchor 의 href + 숫자
    pages: list[tuple[int, str]] = []
    for a in soup.find_all("a", href=True):
        text = a.get_text(" ", strip=True)
        if not _re.fullmatch(r"\d{1,4}", text):
            continue
        n = int(text)
        if not (1 <= n <= 999):
            continue
        abs_url = urljoin(source_url, a["href"])
        p = urlparse(abs_url)
        # 같은 host 만
        if p.netloc != src_p.netloc:
            continue
        pages.append((n, abs_url))

    if len(pages) < 2:
        return None

    # 두 개의 anchor URL 비교 — 다른 곳이 1군데뿐이고 그게 숫자면 그게 page 번호 위치
    # path 를 segment 로 split, 같은 위치 segment 비교
    n1, u1 = pages[0]
    p1 = urlparse(u1)
    seg1 = p1.path.split("/")

    for n2, u2 in pages[1:]:
        if n2 == n1:
            continue
        p2 = urlparse(u2)
        seg2 = p2.path.split("/")
        if len(seg1) != len(seg2):
            continue
        diff_idx = [i for i, (a, b) in enumerate(zip(seg1, seg2)) if a != b]
        if len(diff_idx) != 1:
            continue
        i = diff_idx[0]
        # 그 segment 가 둘 다 숫자만 포함 (e.g., '2', '3' 또는 '2.htm', 'page-2')
        s1, s2 = seg1[i], seg2[i]
        m1 = _re.search(r"\d+", s1)
        m2 = _re.search(r"\d+", s2)
        if not (m1 and m2):
            continue
        if int(m1.group(0)) != n1 or int(m2.group(0)) != n2:
            continue
        # template: i 번째 segment 에서 숫자 부분만 {N} 으로
        templ_seg = s1[:m1.start()] + "{N}" + s1[m1.end():]
        new_segs = list(seg1)
        new_segs[i] = templ_seg
        templ_path = "/".join(new_segs)
        return urlunparse(p1._replace(path=templ_path))

    return None


def _format_path_template(template: str, page_no: int) -> str:
    return template.replace("{N}", str(page_no))


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
        from ..fetchers.dynamic import fetch as _fetch_dynamic
        def _fetch(url, **kwargs):
            return _fetch_dynamic(url)
    else:
        # static 우선, 403 등 anti-scraping 차단 시 dynamic 으로 자동 fallback.
        # 한 번이라도 dynamic 으로 성공하면 그 source 는 dynamic 모드로 stick.
        _session = _make_session()
        _state = {"force_dynamic": False}

        def _fetch(url, **kwargs):
            if _state["force_dynamic"]:
                from ..fetchers.dynamic import fetch as _fetch_dynamic
                return _fetch_dynamic(url)
            headers = kwargs.pop("headers", None) or {}
            headers.setdefault("Referer", source_url)
            r = fetch_static(url, headers=headers, session=_session, **kwargs)
            if r.status == 403 or (r.ok and len(r.text) < 500):
                # anti-scraping 차단 → 30s sleep 후 dynamic fallback
                # (즉시 dynamic 호출 시 사이트가 같은 IP 의 즉시 패턴을 감지하고
                #  decoy HTML 반환하는 케이스 — cambojob 류 — 대응)
                if log:
                    try:
                        log(f"    [fallback] static status={r.status} → sleep 30s 후 dynamic")
                    except Exception:  # noqa: BLE001
                        pass
                import time as _t
                _t.sleep(30)
                from ..fetchers.dynamic import fetch as _fetch_dynamic
                rd = _fetch_dynamic(url)
                if log:
                    try:
                        log(f"    [fallback] dynamic ok={rd.ok} status={rd.status} len={len(rd.text or '')}")
                    except Exception:
                        pass
                if rd.ok:
                    _state["force_dynamic"] = True
                    return rd
            return r

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

    # within-source dedup 제거 — 같은 source 안 같은 공고가 sticky/promoted 형태로
    # 여러 슬롯에 노출되면 그대로 다 yield. DB 에 별개 row 로 적재.
    # cross-batch 증분 break: page 의 모든 raw row 의 external_id 가 seen_ids 안에
    # 있으면 (= 새 공고 0) break.
    page1_new_ids: set[str] = set()      # 이번 page 의 NEW external_id (break 판정용)
    page1_total = 0
    page1_seen_ids: set[str] = set()     # 이번 page 안에서 본 external_id (다음 page learn 판정용)
    for row in _filter_by_prefix(best.rows, prefix):
        page1_total += 1
        eid = _id_of(row.detail_url)
        page1_seen_ids.add(eid)
        if eid in seen_ids:
            continue  # 이미 DB 에 있는 공고 → 적재 X (증분)
        result.rows.append(row)
        page1_new_ids.add(eid)
    result.pages_crawled = 1

    if page1_total == 0:
        result.error = "page 1 rows did not match learned prefix"
        return result

    log(f"    page 1: total={page1_total} new_ids={len(page1_new_ids)} rows_added={sum(1 for r in result.rows)}")
    if seen_ids and not page1_new_ids:
        return result

    # ---- pagination 학습
    # 우선순위: (a) source URL 의 기존 page param → (b) HTML 의 anchor query 분석
    #         → (c) HTML 의 path-segment 패턴 분석 (cambojob 의 /page/N.htm)
    #         → (d) candidate query param 직접 시도 (worldjob 의 pageIndex)
    path_template: Optional[str] = None
    page_param: Optional[str] = None

    detected_param = (
        _detect_existing_page_param(source_url)
        or _detect_page_param_from_html(r.text, r.final_url)
    )
    if detected_param:
        page_param = detected_param
    else:
        # path-segment 학습 우선 (cambojob 케이스)
        path_template = _detect_path_pagination_template(r.text, r.final_url)
        if path_template:
            log(f"    [path pagination] template = {path_template}")
        else:
            # candidate query param 시도
            for cand in PAGINATION_PARAMS:
                tu = _set_query_param(source_url, cand, "2")
                log(f"    [learn page param] try {cand}")
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
                            if row.detail_url and _id_of(row.detail_url) not in page1_seen_ids)
                if t_new >= MIN_ROWS:
                    page_param = cand
                    log(f"    [learn page param] LEARNED: {cand}  (new rows = {t_new})")
                    break
            page_param = page_param or "page"
    result.pagination_param = page_param or ("path:" + (path_template or ""))

    # 같은 페이지 내 sticky 중복은 다 적재. 하지만 다른 페이지에서 같은 ID 다시
    # 보이면 cross-page dedup 으로 skip (siemreap 처럼 사이트가 page query 무시
    # 하고 같은 결과 반복하는 케이스 차단).
    session_seen_ids: set[str] = set(page1_seen_ids)

    # ---- pages 2..N (같은 시그니처 + prefix 매칭만 채택)
    import time as _time
    page_sleep = 0.5     # 정상 사이트는 fast
    for page in range(2, max_pages + 1):
        if path_template:
            page_url = _format_path_template(path_template, page)
        else:
            page_url = _set_query_param(source_url, page_param, str(page))
        _time.sleep(page_sleep)
        log(f"    page {page} fetch... ({page_url[-60:]})")
        rp = _fetch(page_url)

        # 403 받았으면 backoff 늘리고 1회 retry — anti-scraping rate-limit 사이트 대응
        if rp.status == 403 and page_sleep < 30:
            page_sleep = 30
            log(f"    [backoff] 403 받음 → sleep {page_sleep}s 후 retry")
            _time.sleep(page_sleep)
            rp = _fetch(page_url)

        if not rp.ok:
            log(f"    break: page {page} fetch fail (status={rp.status})")
            break

        multi_p = extract_list_multi(rp.text, rp.final_url)
        # 같은 시그니처 컨테이너만 사용
        same_sig = [e for e in multi_p.candidates
                    if _normalize_sig(e.container_signature) == locked_sig]
        if not same_sig:
            log(f"    break: page {page} signature {locked_sig!r} 매칭 컨테이너 없음 "
                f"(found sigs: {[e.container_signature for e in multi_p.candidates[:3]]})")
            break

        page_ext = max(same_sig, key=lambda e: e.count)
        page_rows = _filter_by_prefix(page_ext.rows, prefix)
        if not page_rows:
            log(f"    break: page {page} prefix {prefix!r} 매칭 row 없음 "
                f"(sample row urls: {[r.detail_url[:80] for r in page_ext.rows[:3]]})")
            break

        # 먼저 이 페이지의 ID set 만 추출 (적재 결정 전)
        page_ids: set[str] = set()
        for row in page_rows:
            page_ids.add(_id_of(row.detail_url))

        # break 조건: 이 페이지의 모든 ID 가 이전 페이지에서 이미 봤음
        # → 사이트가 page query 무시하거나 페이지네이션 끝
        if page_ids and page_ids.issubset(session_seen_ids):
            sample_ids = sorted(page_ids)[:5]
            log(f"    break: page {page} 모든 ID 가 이미 봤음 ({len(page_ids)} IDs, sample={sample_ids})")
            break

        # 새 ID 가 일부라도 있으면 이 페이지의 모든 raw row 적재 (sticky 포함)
        # cross-batch 증분: DB 에 이미 있는 ID 만 skip
        page_total = 0
        page_inserted = 0
        for row in page_rows:
            page_total += 1
            eid = _id_of(row.detail_url)
            if eid in seen_ids:
                continue  # cross-batch — DB 에 이미 있는 공고
            result.rows.append(row)
            page_inserted += 1

        session_seen_ids.update(page_ids)
        result.pages_crawled = page
        log(f"    page {page}: total={page_total} added={page_inserted}")

    return result
