"""
사이트 메뉴 자동 탐색

add 시 단일 URL 만 보고 1개 endpoint 만 저장하던 방식의 한계 대응:
사이트의 네비게이션/사이드바 링크를 훑어서 "채용/구인" 관련 메뉴 후보를 반환.
각 메뉴는 이후 analyzer + validator 로 개별 검증되어 sources 배열에 누적된다.

알바몬 같이 메인 홈 API 가 "특별 추천 20건 고정" 인 경우, 메인 한 개만 등록하면
수집이 20건에 멈춤. 이 모듈이 `/jobs`, `/search`, 카테고리별 URL 까지 찾아 추가.

탐색 방법:
  1. Playwright 로 URL 로드 (JS 렌더 후 DOM 취득)
  2. <a href> 수집, 같은 origin + 중복 제거
  3. 텍스트/URL 에 구인 키워드 포함한 것만 필터
  4. 휴리스틱 스코어링: 직접 /jobs 경로가 서브메뉴보다 우선
"""

import os
from typing import Optional
from urllib.parse import urljoin, urlparse


# 구인구직 메뉴 식별용 키워드 — 텍스트 또는 URL 경로에 포함되면 후보.
# 한국어/영어/흔한 축약 패턴 모두 포함.
JOB_MENU_KEYWORDS = (
    # 한글
    "채용", "구인", "구직", "알바", "공고", "모집", "취업", "일자리", "잡",
    "인재", "경력", "신입", "파트타임", "프리랜서", "인턴",
    # 영문
    "job", "jobs", "career", "careers", "recruit", "recruiting", "recruitment",
    "hiring", "hire", "opening", "openings", "position", "positions",
    "employment", "vacancies", "vacancy", "work", "worker",
)

# 제외 키워드 — 이게 포함돼있으면 구인공고가 아닐 가능성 높음 (커뮤니티/문의/로그인/이용약관 등).
JOB_MENU_EXCLUDE = (
    "login", "signin", "signup", "register", "회원가입", "로그인", "가입",
    "terms", "privacy", "policy", "이용약관", "개인정보",
    "faq", "help", "notice", "contact", "문의", "고객센터",
    "about", "회사소개", "기업소개", "company",
    "news", "blog", "community", "커뮤니티", "자유게시판",
)

# 수집 링크 상한 — 메인이 100+ 링크면 무의미하게 커짐. 휴리스틱 스코어 top-N.
MAX_MENU_CANDIDATES = 20


def _score_link(text: str, href: str) -> int:
    """텍스트 + URL 에서 구인 관련성 스코어. 높을수록 구인 가능성."""
    combined = f"{text} {href}".lower()
    if any(kw in combined for kw in JOB_MENU_EXCLUDE):
        return -100  # 배제
    score = 0
    for kw in JOB_MENU_KEYWORDS:
        if kw in combined:
            score += 3
    # URL 경로가 /jobs /recruit /careers 로 **시작**하면 가산
    path = urlparse(href).path.lower()
    for kw in ("/jobs", "/job/", "/recruit", "/career", "/careers", "/search",
              "/list", "/apply", "/positions", "/채용", "/구인"):
        if kw in path:
            score += 4
    # 텍스트가 너무 길면 감점 (본문 링크일 가능성)
    if len(text) > 30:
        score -= 2
    return score


def _extract_links_with_playwright(url: str, timeout_ms: int = 15000) -> list[dict]:
    """Playwright 로 URL 로드 → 모든 <a> 수집 (text, href 포함)."""
    from playwright.sync_api import sync_playwright

    launch_kwargs = {"headless": True}
    proxy_url = os.environ.get("PLAYWRIGHT_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy_url:
        pu = urlparse(proxy_url)
        proxy_cfg = {"server": f"{pu.scheme}://{pu.hostname}:{pu.port}"}
        if pu.username:
            proxy_cfg["username"] = pu.username
        if pu.password:
            proxy_cfg["password"] = pu.password
        launch_kwargs["proxy"] = proxy_cfg

    results: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_load_state("networkidle", timeout=5000)
            except Exception:
                pass  # networkidle 타임아웃은 무시 (SPA 가 계속 polling 할 수 있음)
            anchors = page.evaluate(
                "() => Array.from(document.querySelectorAll('a[href]'))"
                ".map(a => ({text: (a.innerText||a.textContent||'').trim(), href: a.getAttribute('href')}))"
            )
            results = [a for a in anchors if a and a.get("href")]
        finally:
            context.close()
            browser.close()
    return results


def discover_job_menus(root_url: str, timeout_ms: int = 15000) -> list[dict]:
    """사이트의 네비게이션에서 "구인/채용" 메뉴 후보 추출.

    Returns: [{menu_name, url, score}, ...]  score 내림차순. root_url 자체도 포함.
    """
    parsed = urlparse(root_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    try:
        anchors = _extract_links_with_playwright(root_url, timeout_ms)
    except Exception as e:
        print(f"    [menu_discovery] Playwright 실패 — {type(e).__name__}: {e}")
        anchors = []

    candidates: dict[str, dict] = {}  # url → {menu_name, url, score}

    # 항상 root_url 자체 포함 (기존 동작 호환 — 최소 이 한 개는 시도)
    candidates[root_url.rstrip("/")] = {
        "menu_name": "main",
        "url": root_url,
        "score": 1,
    }

    for a in anchors:
        text = (a.get("text") or "").strip()
        href = (a.get("href") or "").strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full = urljoin(root_url, href).split("#")[0]
        # 같은 origin 만
        try:
            p = urlparse(full)
        except Exception:
            continue
        if p.netloc and p.netloc != parsed.netloc:
            continue
        score = _score_link(text, full)
        if score <= 0:
            continue
        key = full.rstrip("/")
        if key in candidates:
            # 더 높은 점수면 갱신
            if score > candidates[key]["score"]:
                candidates[key]["score"] = score
                if text:
                    candidates[key]["menu_name"] = text[:40]
            continue
        candidates[key] = {
            "menu_name": (text[:40] or full.split("/")[-1] or "menu"),
            "url": full,
            "score": score,
        }

    # 스코어 내림차순 정렬 + 상한
    ordered = sorted(candidates.values(), key=lambda x: x["score"], reverse=True)
    return ordered[:MAX_MENU_CANDIDATES]
