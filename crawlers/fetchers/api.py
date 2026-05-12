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


def _render_camhr_body(obj: dict) -> str:
    """camhr.com detail JSON 에서 모든 본문 영역을 HTML 로 렌더링.

    실제 페이지에 보이는 모든 섹션 재현:
      - 메타 표: Level/Term, Year of Exp/Function, Hiring/Industry, Salary/Qualification,
                 Sex/Language, Age/Location, 주소
      - Job Description (description)
      - Job Requirements (requirement)
      - Major / Others Benefit (welfare 는 employer 안)
      - Contact: 회사명/이메일/전화

    detail_content_field 만 쓰면 description 만 나와 본문이 1/3 미만으로 잘림.
    """
    from html import escape

    def _label(d):
        if isinstance(d, dict):
            return str(d.get("label") or d.get("name") or "").strip()
        return ""

    def _esc(v):
        return escape(str(v)) if v is not None else ""

    parts: list[str] = []

    # 헤더 — 게시일/마감일/모집인원
    pub = obj.get("pubdate") or ""
    exp = obj.get("expdate") or obj.get("closeDate") or ""
    pub_date = pub[:10] if pub else ""
    exp_date = exp[:10] if exp else ""
    if pub_date or exp_date:
        parts.append("<p><b>Publish Date:</b> " + _esc(pub_date)
                     + "  <b>Closing Date:</b> " + _esc(exp_date) + "</p>")

    # 메타 표 (사이트와 동일한 행 구성)
    rows = []
    rows.append(("Level", _label(obj.get("jobLevelId"))))
    rows.append(("Term", _label(obj.get("termId"))))
    we = obj.get("workyears")
    rows.append(("Year of Exp", str(we) if we else ""))
    rows.append(("Function", _label(obj.get("categoryId"))))
    hr = obj.get("hirelings")
    rows.append(("Hiring", str(hr) if hr else ""))
    rows.append(("Industry", _label(obj.get("industrialId"))))
    rows.append(("Salary", _label(obj.get("salaryId"))))
    rows.append(("Qualification", _label(obj.get("qualificationId"))))
    rows.append(("Sex", _label(obj.get("sex"))))
    # Language
    lang_parts = []
    for jl in (obj.get("jobLangs") or []):
        if isinstance(jl, dict):
            lang = _label(jl.get("languageId"))
            lvl = _label(jl.get("languageLevelId"))
            if lang:
                lang_parts.append(f"{lang}{(' — ' + lvl) if lvl else ''}")
    rows.append(("Language", ", ".join(lang_parts)))
    # Age
    af, at = obj.get("ageFrom") or 0, obj.get("ageTo") or 0
    age = (f"{af}–{at}" if (af or at) else "Age Unlimited")
    rows.append(("Age", age))
    # Location
    loc_parts = []
    for loc in (obj.get("locations") or []):
        if isinstance(loc, dict):
            l = _label(loc.get("locationId"))
            if l:
                loc_parts.append(l)
    rows.append(("Location", ", ".join(loc_parts)))
    rows.append(("Address", obj.get("address") or ""))
    rows.append(("Major", obj.get("major") or ""))
    table_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>"
        for k, v in rows if v
    )
    if table_rows:
        parts.append(f"<table>{table_rows}</table>")

    # Job Description
    desc = obj.get("description") or ""
    if desc:
        parts.append("<h3>Job Description</h3>")
        parts.append("<pre style='white-space:pre-wrap;font-family:inherit'>"
                     + _esc(desc) + "</pre>")

    # Job Requirements
    req = obj.get("requirement") or ""
    if req:
        parts.append("<h3>Job Requirements</h3>")
        parts.append("<pre style='white-space:pre-wrap;font-family:inherit'>"
                     + _esc(req) + "</pre>")

    # Others Benefit / Welfare (employer 안)
    emp = obj.get("employer") if isinstance(obj.get("employer"), dict) else {}
    welfare = emp.get("welfare") or ""
    if welfare:
        parts.append("<h3>Others Benefit</h3>")
        parts.append("<pre style='white-space:pre-wrap;font-family:inherit'>"
                     + _esc(welfare) + "</pre>")

    # 회사/연락처
    contact = obj.get("contact") if isinstance(obj.get("contact"), dict) else {}
    company = emp.get("company") or ""
    emp_addr = emp.get("address") or ""
    contact_rows = []
    if company:
        contact_rows.append(("Company", company))
    if emp_addr:
        contact_rows.append(("Company Address", emp_addr))
    if contact.get("name"):
        contact_rows.append(("Contact", contact.get("name")))
    if contact.get("email") or obj.get("showEmail"):
        contact_rows.append(("Email", contact.get("email") or emp.get("email") or ""))
    if contact.get("telephone"):
        contact_rows.append(("Phone", contact.get("telephone")))
    if contact_rows:
        parts.append("<h3>Contact</h3>")
        parts.append("<table>" + "".join(
            f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>"
            for k, v in contact_rows if v
        ) + "</table>")

    return "\n".join(parts)


def _render_heykorean_body(data: dict) -> str:
    """heykorean detail JSON 을 HTML 로 렌더링.

    /api/job/info/{id} 응답: top-level {job, company, status}.
    job 안에 content/task/requirement/give_preference/welfare 등 본문 영역,
    company 안에 회사 소개/주소/연락처.
    """
    from html import escape

    job = data.get("job") if isinstance(data.get("job"), dict) else {}
    company = data.get("company") if isinstance(data.get("company"), dict) else {}

    def _esc(v):
        return escape(str(v)) if v is not None else ""

    def _block(title: str, html_or_text: str) -> str:
        if not html_or_text:
            return ""
        # heykorean 의 content/task/etc 는 이미 HTML (<p> tags) — escape 하지 말고 그대로
        return f"<h3>{escape(title)}</h3>\n{html_or_text}\n"

    parts: list[str] = []

    # 메타 표
    rows = []
    title = job.get("title") or ""
    if job.get("ad_start") or job.get("perioid_end"):
        rows.append(("게시일", (job.get("ad_start") or "")[:10]))
        rows.append(("마감일", job.get("perioid_end") or ""))
    if job.get("address"):
        rows.append(("위치", job.get("address")))
    # 급여
    cur = job.get("salary_currency") or ""
    s_min, s_max = job.get("min_salary"), job.get("max_salary")
    if s_min or s_max:
        unit_map = {1: "/시", 2: "/일", 3: "/주", 4: "/월", 5: "/년"}
        unit = unit_map.get(job.get("salary_unit"), "")
        if s_min and s_max and s_min != s_max:
            rows.append(("급여", f"{cur} {s_min:,} ~ {s_max:,}{unit}"))
        elif s_min or s_max:
            rows.append(("급여", f"{cur} {s_min or s_max:,}{unit}"))
    # 경력
    ey = job.get("experience_year") or 0
    if ey:
        rows.append(("경력", f"{ey}년 이상"))
    # 비자 / 인턴 / 재택
    if job.get("sponsor_visa"):
        rows.append(("비자 스폰", "가능"))
    if job.get("internship"):
        rows.append(("인턴십", "가능"))
    if job.get("remote_work"):
        rows.append(("재택근무", "가능"))
    if job.get("tag"):
        rows.append(("태그", job.get("tag")))

    table_rows = "".join(
        f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>"
        for k, v in rows if v
    )
    if table_rows:
        parts.append(f"<table>{table_rows}</table>")

    # 본문 섹션 (이미 HTML 이라 그대로 삽입)
    parts.append(_block("공고 내용", job.get("content") or ""))
    parts.append(_block("주요 업무", job.get("task") or ""))
    parts.append(_block("자격 요건", job.get("requirement") or ""))
    parts.append(_block("우대 사항", job.get("give_preference") or ""))
    parts.append(_block("복리 후생", job.get("welfare") or ""))

    # 회사 정보
    if company:
        c_rows = []
        if company.get("name"): c_rows.append(("회사명", company.get("name")))
        if company.get("industry"): c_rows.append(("업종", company.get("industry")))
        if company.get("size"): c_rows.append(("규모", company.get("size")))
        if company.get("address"):
            addr = " ".join(filter(None, [company.get("address"),
                                           company.get("city"),
                                           company.get("country")]))
            c_rows.append(("주소", addr))
        if company.get("phone"): c_rows.append(("전화", company.get("phone")))
        if company.get("website"): c_rows.append(("홈페이지", company.get("website")))
        about = company.get("about_us") or company.get("about_us_line") or ""
        if c_rows or about:
            parts.append("<h3>회사 정보</h3>")
            if c_rows:
                parts.append("<table>" + "".join(
                    f"<tr><th>{_esc(k)}</th><td>{_esc(v)}</td></tr>"
                    for k, v in c_rows
                ) + "</table>")
            if about:
                parts.append(about)  # already HTML

    # 지원 안내
    if job.get("alram_email"):
        parts.append(f"<p><b>지원 이메일:</b> {_esc(job.get('alram_email'))}</p>")

    return "\n".join(p for p in parts if p)


def fetch_api_detail(schema: ApiSchema, job_id: str | int, *, timeout: int = 15):
    """schema.detail_api_url_template 로 detail JSON 받아서 JobDetail 변환.

    SPA 사이트 (camhr 등) 의 본문 수집 — list 와 동일한 JSON API 패턴 사용.
    """
    from ..batch.detail_crawler import (
        JobDetail, _absolutize_html, _extract_images,
        _extract_links_and_attachments, _extract_iframes_videos,
        _extract_tables, _extract_contacts, _strip_trailing_nav,
        BODY_MAX_CHARS, BODY_HTML_MAX_CHARS,
    )
    from bs4 import BeautifulSoup

    detail_url = (schema.detail_url_template or "").replace("{id}", str(job_id))
    if not schema.detail_api_url_template:
        return JobDetail(url=detail_url, error="no detail_api_url_template")
    api_url = schema.detail_api_url_template.replace("{id}", str(job_id))

    r = fetch_static(api_url, timeout=timeout, headers={"Accept": "application/json"})
    if not r.ok:
        return JobDetail(url=detail_url, error=r.error or f"HTTP {r.status}")
    try:
        data = json.loads(r.text or "{}")
    except Exception as e:  # noqa: BLE001
        return JobDetail(url=detail_url, error=f"json parse fail: {e}")

    obj = get_at_path(data, schema.detail_path or "") if schema.detail_path else data
    if not isinstance(obj, dict):
        return JobDetail(url=detail_url, error="detail object not found")

    # heykorean 은 top-level {job, company, status} 라 detail_path 없이 full data 사용.
    title = ""
    src_for_title = obj
    if "heykorean.com" in api_url and isinstance(data.get("job"), dict):
        src_for_title = data["job"]
    if schema.detail_title_field:
        title = str(src_for_title.get(schema.detail_title_field) or "").strip()
    if not title:
        title = str(src_for_title.get("title")
                    or src_for_title.get(schema.title_field) or "").strip()

    # body — 사이트별 풍부 렌더러 우선, 없으면 일반 fallback
    body_html = ""
    if "camhr.com" in api_url:
        body_html = _render_camhr_body(obj)
    elif "heykorean.com" in api_url:
        # heykorean 은 full data (job + company) 사용
        body_html = _render_heykorean_body(data)
    if not body_html:
        if schema.detail_html_field and obj.get(schema.detail_html_field):
            body_html = str(obj[schema.detail_html_field])
        if not body_html and schema.detail_content_field and obj.get(schema.detail_content_field):
            from html import escape
            body_html = "<pre style='white-space:pre-wrap;font-family:inherit'>" \
                        + escape(str(obj[schema.detail_content_field])) + "</pre>"

    soup = BeautifulSoup(body_html or "", "html.parser")
    _absolutize_html(soup, detail_url)
    body_text = " ".join(soup.get_text(" ", strip=True).split())[:BODY_MAX_CHARS]
    body_text = _strip_trailing_nav(body_text)
    body_html = str(soup)[:BODY_HTML_MAX_CHARS]

    images = _extract_images(soup, detail_url)
    links, attachments = _extract_links_and_attachments(soup, detail_url)
    iframes, videos = _extract_iframes_videos(soup, detail_url)
    tables = _extract_tables(soup)
    emails, phones = _extract_contacts(body_text)

    # API 응답 자체에서 추가 메타 (회사/주소/급여 등) 도 meta 에 보존
    meta = {
        "api_detail": True,
    }
    for k in ("company", "employer", "address", "weburl", "salary", "salaryRange",
              "workyears", "hirelings", "expdate", "pubdate", "requirement",
              "othersQualification", "ageFrom", "ageTo", "cities"):
        if k in obj:
            v = obj[k]
            if isinstance(v, (str, int, float, bool)) or v is None:
                meta[k] = v
            elif isinstance(v, dict):
                meta[k] = {kk: vv for kk, vv in v.items()
                           if isinstance(vv, (str, int, float, bool))}

    return JobDetail(
        url=detail_url, title=title or None,
        raw_text_snippet=body_text, body_html=body_html,
        images=images, links=links, attachments=attachments,
        iframes=iframes, videos=videos,
        emails=emails, phones=phones, tables=tables,
        meta=meta, jsonld=[],
    )
