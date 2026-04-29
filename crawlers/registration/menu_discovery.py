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


_DETAIL_URL_PATTERNS = [
    r"-show-\d+",       # cambojob: jobs-show-22291, news-show-248
    r"/view/\d+",
    r"/detail/\d+",
    r"\bview\.do\b",
    r"\.do\?[^=]*[Ss]eq=\d",
    r"_no=\d",
    r"\?id=\d{4,}",
    r"/post/\d+",
    r"/article/\d+",
    r"/p/\d+",
]
_DETAIL_RE = re.compile("|".join(_DETAIL_URL_PATTERNS))


def _is_detail_like(url: str) -> bool:
    """URL 이 list 가 아닌 detail 페이지처럼 생겼는지."""
    return bool(_DETAIL_RE.search(url))


def _normalize_url_pattern(url: str) -> str:
    """URL 의 숫자/keyword 부분을 placeholder 로 → 같은 패턴 dedup 용.
    예: /jobs/jobs_list/key/客服.htm
        /jobs/jobs_list/key/运营.htm
        → 둘 다 /jobs/jobs_list/key/{KW}.htm
    """
    p = urlparse(url)
    path = re.sub(r"\d+", "{N}", p.path)
    # 마지막 path segment 가 unicode/한자 keyword 류면 placeholder
    path = re.sub(r"/[^\x00-\x7F][^/]*\.htm$", "/{KW}.htm", path)
    path = re.sub(r"/%[A-F0-9]{2}[^/]*\.htm", "/{KW}.htm", path)  # url-encoded
    return f"{p.scheme}://{p.netloc}{path}"


def _fetch_sitemap_links(base_url: str, *, max_candidates: int = 50) -> list[tuple[str, str]]:
    """robots.txt → sitemap.xml 들 따라가서 list 후보가 될 만한 URL 수집.

    필터 정책:
      - detail-like URL 은 제외 (jobs-show-*, news-show-* 등)
      - 같은 normalized path pattern 은 1개만 (cambojob 의 keyword 별 list 들 dedup)
      - 최대 max_candidates 개로 cap (validate 비용 절약)
    """
    from urllib.parse import urljoin
    raw: list[str] = []

    def _parse_sitemap(xml_text: str, depth: int = 0) -> None:
        if depth > 2:
            return
        locs = re.findall(r"<loc>([^<]+)</loc>", xml_text)
        for loc in locs:
            loc = loc.strip()
            if loc.endswith(".xml") and "sitemap" in loc.lower():
                rs = fetch(loc, timeout=10)
                if rs.ok:
                    _parse_sitemap(rs.text, depth + 1)
            else:
                raw.append(loc)

    # 1) robots.txt 에서 sitemap path 추출
    robots = fetch(urljoin(base_url, "/robots.txt"), timeout=10)
    sitemaps: list[str] = []
    if robots.ok:
        for m in re.finditer(r"^\s*Sitemap:\s*(\S+)", robots.text, re.I | re.M):
            sitemaps.append(m.group(1).strip())
    if not sitemaps:
        sitemaps = [urljoin(base_url, "/sitemap.xml")]

    for sm in sitemaps[:3]:
        rs = fetch(sm, timeout=10)
        if rs.ok and "<loc>" in rs.text:
            _parse_sitemap(rs.text)

    # 필터:
    #  - detail-like URL 제외 (jobs-show-N 등)
    #  - url-encoded path 제외 (cambojob 의 /key/한자.htm 같은 keyword 필터 변종)
    #  - 같은 normalized path pattern 은 1개만
    seen_patterns: set[str] = set()
    out: list[tuple[str, str]] = []
    for u in raw:
        if _is_detail_like(u):
            continue
        if "%" in urlparse(u).path:
            continue
        pat = _normalize_url_pattern(u)
        if pat in seen_patterns:
            continue
        seen_patterns.add(pat)
        out.append((u, ""))
        if len(out) >= max_candidates:
            break
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
    # static 차단 시 proxy 풀로 fallback (슈퍼루키 류 anti-scraping)
    if not home.ok or home.status == 403:
        from ..infra.config import PROXIES
        if PROXIES:
            for p in PROXIES:
                hp = fetch(home_url, proxy=p)
                if hp.ok:
                    home = hp
                    break
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

    # sitemap.xml 보강 — 홈 anchor 에 없는 채용 list URL 도 추가 후보로
    # (cambojob 처럼 SPA landing 에 채용 메뉴 자체가 노출 안 되는 사이트 대응)
    try:
        sitemap_links = _fetch_sitemap_links(home.final_url)
        if sitemap_links:
            sitemap_cands = _filter_candidates(sitemap_links, home.final_url, via="sitemap")
            existing_urls = {c.url for c in candidates}
            for c in sitemap_cands:
                if c.url not in existing_urls:
                    candidates.append(c)
            candidates.sort(key=lambda c: -c.score)
    except Exception:  # noqa: BLE001
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
