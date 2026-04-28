"""홈 URL → 메뉴 디스커버리 → 분류 → 검증 → dedupe → sites 저장.

흐름 (orchestrator):
  1. discover(home_url)            -> 후보 N개
  2. classify_batch(top_K)         -> full / filtered / personal / unknown
  3. full + filtered 만 validate()  (filtered 는 full 이 없을 때 fallback)
  4. dedupe(passed)                -> 겹치는 메뉴 흡수
  5. sites_repo.upsert_site(status='active', sources=[...])

실패 시 status='pending' 으로 남겨둠 (재시도 가능).
홈 fetch 자체가 죽으면 status='dead' (도메인 사망).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import re

from ..infra.sites_repo import upsert_site
from .menu_classifier import ClassifyInput, ClassifyResult, classify_batch
from .menu_dedupe import DedupeDecision, dedupe
from .menu_discovery import DiscoveryResult, discover
from .menu_validator import ValidationResult, validate
from .site_id import extract_site_id


DEFAULT_TOP = 300  # 사실상 무제한 (모든 메뉴를 분류 대상으로)
MIN_SCORE = 2      # score 1 (대부분 search/footer 노이즈) 은 제외

# 후보 텍스트가 짧은 메뉴 라벨이고 아래 키워드를 포함하면
# LLM 의 unknown 분류를 filtered 로 강제 override.
# (LLM 이 이런 명백한 메뉴를 가끔 unknown 으로 떨어뜨리는 케이스 보강)
_OVERRIDE_TEXT_MAX_LEN = 20
_OVERRIDE_KEYWORDS_RE = re.compile(
    "|".join([
        "구인구직", "구인", "구직", "채용공고", "채용정보", "채용", "모집공고",
        "career", "careers", "recruit", "recruiting", "recruitment",
        r"\bjobs?\b", "hiring", "vacancies",
    ]),
    re.I,
)


@dataclass
class RegisterReport:
    home_url: str
    site_id: str
    final_status: str
    discovery: Optional[DiscoveryResult] = None
    classifications: list[ClassifyResult] = field(default_factory=list)
    validations: list[ValidationResult] = field(default_factory=list)
    dedupe: Optional[DedupeDecision] = None
    sources: list[dict] = field(default_factory=list)  # 최종 등록된 sources
    notes: list[str] = field(default_factory=list)


def _build_source(v: ValidationResult, label: str) -> dict:
    src = {
        "url": v.url,
        "final_url": v.final_url,
        "label": label,
        "list_rows": v.list_rows,
        "subject_link_ratio": round(v.subject_link_ratio, 2),
        "container_signature": v.container_signature,
        "fetcher": v.fetcher,
    }
    if v.api_schema:
        src["api_schema"] = v.api_schema
    if v.total_count is not None:
        src["total_count"] = v.total_count
    return src


def _extract_site_name(html: str) -> Optional[str]:
    """홈 HTML 에서 사이트 이름 추출.

    우선순위:
      1. og:site_name (가장 정확)
      2. og:title 첫 segment ('CamHR - Find jobs ...' 같이 사이트명-슬로건 형식)
      3. <title> 의 segment 중 가장 짧은 것
         (SEO 페이지는 'X|Y|Z|... -- SiteName' 처럼 사이트명이 끝에 오기도 함)
    """
    import re

    def _segments(s: str) -> list[str]:
        # `|`, ` - `, ` – `, ` -- `, ` :: ` 로 split
        parts = re.split(r"\s*\|\s*|\s+[-–]{1,2}\s+|\s+::\s+", s)
        return [p.strip() for p in parts if p.strip()]

    m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if m:
        v = m.group(1).strip()
        if v:
            return v[:80]

    m = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', html, re.I)
    if m:
        segs = _segments(re.sub(r"\s+", " ", m.group(1)))
        if segs:
            return segs[0][:80]

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", m.group(1)).strip()
        segs = _segments(title)
        if segs:
            # 가장 짧은 segment = 사이트명일 가능성 높음 (SEO 키워드는 김)
            best = min(segs, key=len)
            return best[:80]
    return None


def register(
    home_url: str,
    *,
    name: Optional[str] = None,
    top_n: int = DEFAULT_TOP,
    use_snippet: bool = False,
    dry_run: bool = False,
) -> RegisterReport:
    site_id = extract_site_id(home_url)
    rep = RegisterReport(home_url=home_url, site_id=site_id, final_status="pending")

    # 1. discover
    d = discover(home_url, depth1_top_n=5)
    rep.discovery = d

    # 사이트 이름: 인자로 안 들어오면 discovery 가 받은 HTML 에서 자동 추출
    # (dynamic fallback 으로 받아온 html 도 그대로 사용 — SPA 사이트도 커버)
    if not name and d.ok and d.home_html:
        name = _extract_site_name(d.home_html)

    if not d.ok:
        rep.final_status = "dead"
        rep.notes.append(f"home fetch failed: {d.error}")
        if not dry_run:
            upsert_site(
                site_id, home_url, name=name,
                status="dead", status_reason=f"home fetch failed: {d.error}",
                sources=[],
            )
        return rep

    if not d.candidates:
        rep.final_status = "pending"
        rep.notes.append("no menu candidates discovered")
        if not dry_run:
            upsert_site(
                site_id, home_url, name=name,
                status="pending", status_reason="no_menu_candidates",
                sources=[],
            )
        return rep

    # 2. classify — score >= MIN_SCORE 인 모든 후보를 분류 (top_n 은 안전 상한)
    candidates = [c for c in d.candidates if c.score >= MIN_SCORE][:top_n]
    items = [ClassifyInput(url=c.url, text=c.text) for c in candidates]
    classifications = classify_batch(items, fetch_snippet=use_snippet)

    # LLM unknown override: 짧은 메뉴 라벨 + 강 키워드 매칭이면 filtered 로 강제
    text_by_url = {c.url: c.text for c in candidates}
    for r in classifications:
        if r.label != "unknown":
            continue
        t = (text_by_url.get(r.url) or "").strip()
        if 0 < len(t) <= _OVERRIDE_TEXT_MAX_LEN and _OVERRIDE_KEYWORDS_RE.search(t):
            r.label = "filtered"
            r.reason = (r.reason or "") + " [override: keyword in short label]"

    rep.classifications = classifications

    full_urls   = [r.url for r in classifications if r.label == "full"]
    filt_urls   = [r.url for r in classifications if r.label == "filtered"]

    # 3. validate — full 과 filtered "모두" 검증 대상
    #    (모든 공고 수집을 위해 full 만으로 부족할 수 있음 → 카테고리도 함께)
    target_urls = full_urls + filt_urls
    label_for = {u: "full" for u in full_urls}
    for u in filt_urls:
        label_for.setdefault(u, "filtered")

    validations: list[ValidationResult] = []
    for url in target_urls:
        validations.append(validate(url))
    rep.validations = validations

    passed = [v for v in validations if v.ok]
    if not passed:
        rep.final_status = "pending"
        rep.notes.append(
            f"validation: 0 passed (full={len(full_urls)}, filtered={len(filt_urls)})"
        )
        if not dry_run:
            upsert_site(
                site_id, home_url, name=name,
                status="pending", status_reason="validation_failed",
                sources=[],
            )
        return rep

    # 4. dedupe
    decision = dedupe(passed)
    rep.dedupe = decision

    sources = [_build_source(v, label_for.get(v.url, "full")) for v in decision.kept]
    rep.sources = sources

    # 5. save
    rep.final_status = "active"
    if not dry_run:
        upsert_site(
            site_id, home_url, name=name,
            status="active",
            status_reason=None,
            sources=sources,
        )
    return rep
