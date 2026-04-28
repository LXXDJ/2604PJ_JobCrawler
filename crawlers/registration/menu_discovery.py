"""홈 URL → 채용 메뉴 후보 URL 추출.

흐름:
  1. 홈 fetch (curl_cffi)
  2. <a> 전수 수집
  3. 키워드/휴리스틱으로 채용 후보 필터
  4. 필요 시 1-depth 추가 탐색 (홈에서 못 찾으면 about/sitemap 등)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from ..fetchers.static import FetchResult, fetch


# 채용 페이지로 이어지는 텍스트/URL 키워드
KEYWORDS_KO = [
    "채용", "공고", "모집", "입사", "지원", "인재", "커리어", "채용공고",
    "리크루팅", "리쿠르팅", "인재영입",
    "구인", "구직", "구인구직", "잡", "취업", "일자리", "채용정보",
]
KEYWORDS_EN = [
    "career", "careers", "recruit", "recruiting", "recruitment", "job",
    "jobs", "hiring", "employment", "vacancy", "vacancies", "join-us",
    "joinus", "work-with-us", "apply", "position", "positions",
    "opening", "openings", "opportunity", "opportunities",
]
ALL_KEYWORDS = [k.lower() for k in KEYWORDS_KO + KEYWORDS_EN]

# 명백히 채용과 무관한 패턴 (제외)
NEGATIVE_HINTS = [
    "javascript:", "mailto:", "tel:", "#",
    "/login", "/signup", "/privacy", "/terms",
]


@dataclass
class MenuCandidate:
    url: str
    text: str
    score: int = 0
    via: str = "home"  # home / depth1 / sitemap

    def to_dict(self) -> dict:
        return {"url": self.url, "text": self.text, "score": self.score, "via": self.via}


@dataclass
class DiscoveryResult:
    home_url: str
    final_home_url: str
    candidates: list[MenuCandidate] = field(default_factory=list)
    home_html: str = ""   # 메뉴 추출에 사용된 HTML (정적 / dynamic fallback 결과)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def _normalize_url(base: str, href: str) -> Optional[str]:
    if not href:
        return None
    href = href.strip()
    low = href.lower()
    for bad in NEGATIVE_HINTS:
        if low.startswith(bad):
            return None
    abs_url = urljoin(base, href)
    parsed = urlparse(abs_url)
    if parsed.scheme not in ("http", "https"):
        return None
    # fragment 제거
    return parsed._replace(fragment="").geturl()


def _is_same_site(base: str, url: str) -> bool:
    a = urlparse(base).netloc.lower()
    b = urlparse(url).netloc.lower()
    if not a or not b:
        return False
    # 서브도메인 허용 — 같은 등록 도메인이면 True
    a_root = ".".join(a.split(".")[-2:])
    b_root = ".".join(b.split(".")[-2:])
    return a_root == b_root


def _score_link(text: str, url: str) -> int:
    """text/url 에 채용 키워드가 얼마나 있는지 점수화."""
    score = 0
    blob = f"{text} {url}".lower()
    for kw in ALL_KEYWORDS:
        if kw in blob:
            score += 2 if kw in text.lower() else 1
    # 짧은 명시적 단어가 url path 마지막에 있으면 가산
    path = urlparse(url).path.lower().rstrip("/").split("/")[-1]
    if path in ALL_KEYWORDS:
        score += 3
    return score


def _extract_links(html: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    for a in soup.find_all("a", href=True):
        url = _normalize_url(base_url, a["href"])
        if not url:
            continue
        text = (a.get_text(separator=" ", strip=True) or "")[:120]
        out.append((url, text))
    return out


def _filter_candidates(
    links: list[tuple[str, str]],
    base_url: str,
    *,
    via: str,
) -> list[MenuCandidate]:
    seen: dict[str, MenuCandidate] = {}
    for url, text in links:
        if not _is_same_site(base_url, url):
            continue
        score = _score_link(text, url)
        if score <= 0:
            continue
        existing = seen.get(url)
        if existing is None or score > existing.score:
            seen[url] = MenuCandidate(url=url, text=text, score=score, via=via)
    return sorted(seen.values(), key=lambda c: -c.score)


def discover(home_url: str, *, depth1_top_n: int = 0) -> DiscoveryResult:
    """홈에서 채용 메뉴 후보 추출.

    depth1_top_n > 0 이면 홈에서 점수 높은 페이지를 추가로 1-depth 탐색.

    정적 결과가 빈약 (HTML 너무 짧거나 후보 0개) 하면 dynamic fetch (Playwright)
    로 fallback. EPS, SPA 홈처럼 JS 가 메뉴를 렌더하는 사이트 대응.
    """
    home: FetchResult = fetch(home_url)
    if not home.ok:
        return DiscoveryResult(home_url, home.final_url, error=home.error or f"HTTP {home.status}")

    links = _extract_links(home.text, home.final_url)
    candidates = _filter_candidates(links, home.final_url, via="home")

    # SPA fallback: static 결과가 너무 작거나 후보 0개면 dynamic 으로 재시도
    if (not candidates) or (len(home.text or "") < 2000):
        try:
            from ..fetchers.dynamic import fetch as fetch_dynamic
            rd = fetch_dynamic(home_url)
            if rd.ok and len(rd.text or "") > len(home.text or ""):
                home = rd  # final_url 도 갱신
                links = _extract_links(rd.text, rd.final_url)
                candidates = _filter_candidates(links, rd.final_url, via="home")
        except Exception:
            pass

    if depth1_top_n > 0 and not candidates:
        # 홈에서 못 찾으면 점수 0이지만 같은 사이트인 링크 일부를 1-depth 로 따라가본다.
        same_site_links = [
            (u, t) for u, t in links if _is_same_site(home.final_url, u)
        ][:depth1_top_n]
        for sub_url, _ in same_site_links:
            sub = fetch(sub_url)
            if not sub.ok:
                continue
            sub_links = _extract_links(sub.text, sub.final_url)
            extra = _filter_candidates(sub_links, home.final_url, via="depth1")
            for c in extra:
                if c.url not in {x.url for x in candidates}:
                    candidates.append(c)
        candidates.sort(key=lambda c: -c.score)

    return DiscoveryResult(
        home_url=home_url,
        final_home_url=home.final_url,
        candidates=candidates,
        home_html=home.text or "",
    )
