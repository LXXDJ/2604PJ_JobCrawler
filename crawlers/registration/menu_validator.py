"""LLM 분류된 메뉴를 fetch 해서 list 인지 검증.

extractors.list_extractor 가 실제 로직을 담당. 여기서는 임계값만 본다.

검증 항목:
  1. list_rows >= MIN_ROWS
  2. subject_link_ratio >= MIN_SUBJ_RATIO
  3. row 제목의 채용 키워드 비율 >= MIN_JOB_TITLE_RATIO
     - LLM 이 메뉴 라벨만 보고 잘못 분류한 경우 (예: 게시판 자체는 "안전지원단"
       이지만 글들은 채용 무관) 를 자동으로 걸러냄.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

from ..extractors.list_extractor import ListExtraction, extract_list, extract_list_multi
from ..fetchers.dispatcher import fetch as dispatch_fetch
from ..fetchers.static import fetch as fetch_static


MIN_ROWS = 2
MIN_SUBJ_RATIO = 0.5
MIN_JOB_TITLE_RATIO = 0.3   # row 제목 중 채용 키워드 1개 이상 hit 비율
MIN_JOB_TITLE_COUNT = 2     # 비율 미달이어도 절대 갯수 N 이상이면 통과
                             # (광고 도배된 게시판이라도 실제 공고 N개 있으면 의미 있음)
# 진짜 채용 list 는 detail URL 들이 같은 path + ID 만 다름 (unique_path_ratio ≈ 0).
# 메뉴 list (절차안내 등) 는 path 자체가 모두 달라 ≈ 1.0.
# 이 임계 미만이어야 list 로 인정.
MAX_UNIQUE_PATH_RATIO = 0.5


# row 제목에서 "채용 공고" 임을 시사하는 키워드.
# 단순 매칭 (regex), LLM 호출 없음.
_JOB_TITLE_KEYWORDS = [
    # 한국어 — 핵심
    "채용", "모집", "공고", "구인", "구직", "채용공고", "구인구직",
    "입사", "취업", "일자리", "리쿠르팅", "리크루팅",
    # 한국어 — 직무/역할
    "사원", "직원", "인턴", "신입", "경력", "정규직", "계약직",
    "아르바이트", "알바", "강사", "연구원", "매니저", "팀장",
    "선임", "수석", "대리", "과장", "차장", "부장",
    "담당자", "책임자",
    # 영어 — 핵심
    "hiring", "recruit", "position", "opening", "vacancy",
    # 영어 — 직무/역할 (Executive, Manager, Sales 등 일반 직무명)
    "intern", "manager", "engineer", "developer", "designer",
    "executive", "officer", "specialist", "analyst", "supervisor",
    "assistant", "coordinator", "consultant", "programmer",
    "sales", "marketing", "finance", "accountant", "technician",
    "operator", "associate", "lead", "head", "chief", "staff",
    "director", "administrator",
]
_JOB_TITLE_RE = re.compile("|".join(re.escape(k) for k in _JOB_TITLE_KEYWORDS), re.I)


# 이벤트 / 안내 / 설명회 list — 채용 list 아님.
# row 의 절반 이상이 이런 키워드 포함하면 reject.
_EVENT_KEYWORDS = [
    # 한국어
    "설명회", "박람회", "세미나", "강연", "강의", "특강",
    "워크숍", "워크샵", "토론회", "컨퍼런스", "심포지엄", "포럼",
    "행사", "교육과정", "교육안내", "교육일정", "수강",
    "안내", "프로그램 안내",
    # 영어
    "seminar", "webinar", "workshop", "conference", "symposium",
    "info session", "briefing", "fair", "expo",
]
_EVENT_RE = re.compile("|".join(re.escape(k) for k in _EVENT_KEYWORDS), re.I)
MAX_EVENT_RATIO = 0.5   # row 절반 이상이 event 키워드면 reject


def _row_has_job_kw(r) -> bool:
    """row 의 title 또는 row_text 중 하나라도 채용 키워드 매칭."""
    blob = " ".join(filter(None, [getattr(r, "title", ""), getattr(r, "row_text", "")]))
    return bool(blob) and bool(_JOB_TITLE_RE.search(blob))


def _row_is_event(r) -> bool:
    blob = " ".join(filter(None, [getattr(r, "title", ""), getattr(r, "row_text", "")]))
    return bool(blob) and bool(_EVENT_RE.search(blob))


def _event_ratio(rows) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if _row_is_event(r)) / len(rows)


def _job_title_ratio(rows) -> float:
    if not rows:
        return 0.0
    return sum(1 for r in rows if _row_has_job_kw(r)) / len(rows)


def _job_title_count(rows) -> int:
    return sum(1 for r in rows if _row_has_job_kw(r))


def title_is_job_post(title: str | None) -> bool:
    """row 제목 1개가 '채용 공고로 보이는지' 판단.
    runner 가 적재 시 row 필터링에 사용."""
    if not title:
        return False
    return bool(_JOB_TITLE_RE.search(title))


@dataclass
class ValidationResult:
    url: str
    final_url: str
    ok: bool
    list_rows: int = 0
    subject_link_ratio: float = 0.0
    job_title_ratio: float = 0.0
    sample_links: list[str] = field(default_factory=list)
    sample_titles: list[str] = field(default_factory=list)
    container_signature: str = ""
    fetcher: str = "static"   # 'static' | 'dynamic' | 'api'
    api_schema: Optional[dict] = None  # fetcher='api' 일 때 schema dict
    total_count: Optional[int] = None  # API 가 알려주는 전체 공고 수 (있으면)
    error: Optional[str] = None
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "final_url": self.final_url,
            "ok": self.ok,
            "list_rows": self.list_rows,
            "subject_link_ratio": round(self.subject_link_ratio, 2),
            "job_title_ratio": round(self.job_title_ratio, 2),
            "container_signature": self.container_signature,
            "sample_links": self.sample_links[:5],
            "sample_titles": self.sample_titles[:5],
            "error": self.error,
            "reason": self.reason,
        }


def _eval_extraction(ext: ListExtraction) -> tuple[int, float]:
    n_total = ext.count
    n_with_link = sum(1 for r in ext.rows if r.detail_url)
    if n_total == 0:
        return 0, 0.0
    return n_total, n_with_link / n_total


def _unique_path_ratio(rows) -> float:
    """detail URL 들의 path-만 고유성 비율. 진짜 list 는 ≈0, 메뉴 list 는 ≈1."""
    from urllib.parse import urlparse
    paths = [urlparse(r.detail_url).path for r in rows if r.detail_url]
    if not paths:
        return 1.0
    return len(set(paths)) / len(paths)


def _passes_thresholds(ext: ListExtraction | None) -> bool:
    if ext is None or ext.count < MIN_ROWS:
        return False
    _, subj_ratio = _eval_extraction(ext)  # (n_total, subject_link_ratio)
    if subj_ratio < MIN_SUBJ_RATIO:
        return False
    title_ratio = _job_title_ratio(ext.rows)
    title_count = _job_title_count(ext.rows)
    if title_ratio < MIN_JOB_TITLE_RATIO and title_count < MIN_JOB_TITLE_COUNT:
        return False
    if _unique_path_ratio(ext.rows) > MAX_UNIQUE_PATH_RATIO:
        return False
    if _event_ratio(ext.rows) > MAX_EVENT_RATIO:
        return False
    return True


def _pick_passing(html: str, base_url: str) -> ListExtraction | None:
    """모든 list 후보 컨테이너 중 임계 통과하는 것 중 가장 큰 것 선택.
    통과하는 게 없으면 None.

    이유: 페이지에 sidebar 학교목록 (84 rows) + 본문 채용 list (10 rows) 가
    공존할 때 .best 는 sidebar 를 잡지만 그건 채용 키워드 0% → fail.
    실제 채용 컨테이너가 더 작아도 임계 통과하면 그걸 채택해야 함.
    """
    multi = extract_list_multi(html, base_url)
    passing = [e for e in multi.candidates if _passes_thresholds(e)]
    if not passing:
        return None
    return max(passing, key=lambda e: e.count)


def validate(url: str, *, timeout: int = 20) -> ValidationResult:
    # 1차: 정적 fetch — 통과하는 후보 컨테이너 우선 탐색
    r = fetch_static(url, timeout=timeout)
    fetcher_used = "static"
    api_schema_dict = None
    total_count = None
    if r.ok:
        passed = _pick_passing(r.text, r.final_url)
        ext = passed or extract_list(r.text, r.final_url)
    else:
        ext = None

    # 정적 결과가 검증을 통과 못 하면 동적 fallback (Network 캡처 켜기)
    if not _passes_thresholds(ext):
        from ..fetchers.dynamic import fetch as fetch_dynamic
        from ..extractors.api_schema import detect_schema, get_at_path
        rd = fetch_dynamic(url, capture_api=True)
        if rd.ok:
            ed_dyn = _pick_passing(rd.text, rd.final_url) or extract_list(rd.text, rd.final_url)
            api_calls = getattr(rd, "api_calls", None) or []
            xhr_html = getattr(rd, "xhr_html", None) or []

            # 우선순위 1) JSON API — 자동 페이지네이션 가능 + 가장 빠름
            sample_url = ed_dyn.rows[0].detail_url if ed_dyn and ed_dyn.rows else None
            schema = detect_schema(api_calls, sample_detail_url=sample_url,
                                   desired_page_size=50)
            if schema:
                fetcher_used = "api"
                api_schema_dict = schema.to_dict()
                ext = ed_dyn  # API 가 따로 fetch 하므로 page 1 결과로 OK
                r = rd
                for call in api_calls:
                    if call.get("url", "").startswith(schema.base_url):
                        tot = get_at_path(call.get("data") or {}, "data.totalCount") \
                              or (call.get("data") or {}).get("totalCount")
                        if tot is not None:
                            try:
                                total_count = int(tot)
                            except (ValueError, TypeError):
                                pass
                            break
            else:
                # 우선순위 2) XHR HTML endpoint — 정적 GET 으로 같은 list 받을 수 있으면
                # 매 페이지 Playwright 띄우는 비용 회피
                best_xhr = None
                for x in xhr_html:
                    cand = _pick_passing(x["text"], x["url"])
                    if cand is None:
                        continue
                    if best_xhr is None or cand.count > best_xhr[0].count:
                        best_xhr = (cand, x)

                if best_xhr is not None:
                    cand, x = best_xhr
                    from ..fetchers.static import FetchResult
                    r = FetchResult(url=x["url"], status=x["status"],
                                     text=x["text"], final_url=x["url"])
                    ext = cand
                    fetcher_used = "static"
                    url = x["url"]
                elif _passes_thresholds(ed_dyn):
                    # 우선순위 3) dynamic 페이지 그대로 — 느리지만 작동
                    r = rd
                    ext = ed_dyn
                    fetcher_used = "dynamic"

    if not r.ok or ext is None:
        return ValidationResult(
            url=url, final_url=r.final_url, ok=False, fetcher=fetcher_used,
            error=r.error or f"HTTP {r.status}",
        )
    n, ratio = _eval_extraction(ext)
    samples = [row.detail_url for row in ext.rows[:5] if row.detail_url]
    titles = [row.title for row in ext.rows[:5] if row.title]
    title_ratio = _job_title_ratio(ext.rows)

    base = dict(
        url=url, final_url=r.final_url,
        list_rows=n, subject_link_ratio=ratio, job_title_ratio=title_ratio,
        container_signature=ext.container_signature,
        sample_links=samples, sample_titles=titles,
        fetcher=fetcher_used,
        api_schema=api_schema_dict,
        total_count=total_count,
    )

    if n < MIN_ROWS:
        return ValidationResult(**base, ok=False, reason=f"list_rows {n} < {MIN_ROWS}")
    if ratio < MIN_SUBJ_RATIO:
        return ValidationResult(**base, ok=False,
                                reason=f"subject_link_ratio {ratio:.2f} < {MIN_SUBJ_RATIO}")
    title_count = _job_title_count(ext.rows)
    if title_ratio < MIN_JOB_TITLE_RATIO and title_count < MIN_JOB_TITLE_COUNT:
        return ValidationResult(**base, ok=False,
                                reason=(f"job_title_ratio {title_ratio:.2f} < {MIN_JOB_TITLE_RATIO} "
                                        f"and count {title_count} < {MIN_JOB_TITLE_COUNT}"))
    upr = _unique_path_ratio(ext.rows)
    if upr > MAX_UNIQUE_PATH_RATIO:
        return ValidationResult(**base, ok=False,
                                reason=(f"unique_path_ratio {upr:.2f} > {MAX_UNIQUE_PATH_RATIO} "
                                        "(detail URL 들이 모두 다른 경로 → list 가 아닌 메뉴)"))
    er = _event_ratio(ext.rows)
    if er > MAX_EVENT_RATIO:
        return ValidationResult(**base, ok=False,
                                reason=(f"event_ratio {er:.2f} > {MAX_EVENT_RATIO} "
                                        "(설명회/세미나/강의 등 이벤트 list)"))

    return ValidationResult(**base, ok=True)
