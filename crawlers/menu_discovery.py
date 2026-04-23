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
import re
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

# 수집 링크 상한 — 키워드 필터 + 패턴 중복 제거 후에도 남는 후보 수.
# 사용자 요구: "모든 메뉴 다 들어가서 구인 공고 있는지 확인". 시간 비용 크지만 초기 add
# 한 번만 하면 되므로 넉넉히. 패턴 중복제거가 잘 되면 보통 20~50개 수준.
MAX_MENU_CANDIDATES = 200


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


def _extract_links_with_playwright(url: str, timeout_ms: int = 45000) -> list[dict]:
    """Playwright 로 URL 로드 → 네비게이션 영역의 링크만 수집.

    본문/광고/푸터 링크는 제외하고 "메뉴" 역할의 anchor 만 대상.
    식별 기준 (우선순위):
      1. <nav>, <header>, <aside>, [role="navigation"]
      2. 흔한 메뉴 클래스: gnb/lnb/nav/navbar/menu/sidebar/gnb-menu/main-menu/category 등
      3. fallback: body 의 상단 15% viewport 안 링크 (메가메뉴가 일반 div 안에 있을 때)
    """
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

    USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )
    results: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(
            ignore_https_errors=True,
            user_agent=USER_AGENT,
            locale="ko-KR",
            viewport={"width": 1400, "height": 900},
        )
        # 헤드리스 시그널 숨기기 — 기본 headless Chromium 은 navigator.webdriver=true
        # 로 표시돼 사이트가 차단 (사람인 등). stealth_fetcher 와 같은 기법.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        try:
            # commit: 첫 바이트 도착하면 OK. domcontentloaded 이면 SPA 의 초기 JS 번들
            # 전부 받아야 해서 사람인 같은 무거운 홈은 20s 도 부족. commit 후 고정 대기로 전환.
            try:
                page.goto(url, wait_until="commit", timeout=timeout_ms)
            except Exception:
                # 그래도 timeout 나면 최소 body 로딩 시도
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    pass
            # DOM 렌더/네비 펼쳐지길 기다림
            try:
                page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass  # networkidle 타임아웃은 무시 (SPA 가 계속 polling 할 수 있음)

            # === 메가메뉴 펼치기: top-level nav 항목에 hover ===
            # 알바몬/사람인 등은 hover 시에만 메가메뉴 DOM 을 동적 렌더. hover 안 하면
            # 서브메뉴(지역별/업직종별/...) 는 DOM 에 존재하지 않아 querySelectorAll 이 못 잡음.
            TOP_MENU_SELS = [
                'nav > ul > li', 'nav > ol > li',
                'header nav > ul > li', 'header nav > ol > li',
                'header > nav > ul > li',
                '.gnb > ul > li', '.gnb-menu > li', '.gnb li.gnb-item',
                '.nav > li', '.navbar > li', '.main-menu > li',
                '[class*="Gnb"] > ul > li', '[class*="GNB"] > ul > li',
                '[class*="gnb"] > ul > li', '[class*="Menu"] > ul > li',
                '[role="menubar"] > [role="menuitem"]',
            ]
            for sel in TOP_MENU_SELS:
                try:
                    items = page.query_selector_all(sel)
                except Exception:
                    continue
                for item in items[:15]:  # top-level 보통 5~10개, 상한 15
                    try:
                        item.hover(timeout=1500)
                        page.wait_for_timeout(350)  # 드롭다운 애니메이션/lazy render 대기
                    except Exception:
                        pass

            anchors = page.evaluate(
                """() => {
                    const NAV_SELECTORS = [
                        'nav', 'header', 'aside', '[role="navigation"]',
                        '.gnb', '.lnb', '.nav', '.navbar', '.navigation',
                        '.menu', '.main-menu', '.header-menu', '.site-menu',
                        '.sidebar', '.side-menu', '.category', '.categories',
                        '[class*="Nav"]', '[class*="Menu"]', '[class*="gnb"]',
                        '[class*="navi"]',
                        // 메가메뉴/드롭다운 — hover 시 DOM 에 추가되는 서브메뉴 영역
                        '[class*="MegaMenu"]', '[class*="mega-menu"]', '[class*="mega_menu"]',
                        '[class*="DropDown"]', '[class*="dropdown"]', '[class*="drop-down"]',
                        '[class*="submenu"]', '[class*="sub-menu"]', '[class*="subNav"]',
                        '[class*="SubNav"]', '[class*="Subnav"]',
                        '[class*="Category"]', '[class*="GNB"]', '[class*="LNB"]',
                        '[class*="sitemap"]', '[class*="Sitemap"]',
                    ];
                    const seen = new Set();
                    const out = [];
                    const pushA = (a) => {
                        const href = a.getAttribute('href');
                        if (!href) return;
                        const key = href;
                        if (seen.has(key)) return;
                        seen.add(key);
                        out.push({
                            text: (a.innerText || a.textContent || '').replace(/\\s+/g,' ').trim(),
                            href: href,
                        });
                    };
                    // 1차: 명시적 네비 컨테이너
                    for (const sel of NAV_SELECTORS) {
                        for (const container of document.querySelectorAll(sel)) {
                            for (const a of container.querySelectorAll('a[href]')) {
                                pushA(a);
                            }
                        }
                    }
                    // 2차 fallback: 네비 컨테이너 매치가 너무 적으면 body 전체 <a> 스캔.
                    // 사람인/잡코리아 처럼 커스텀 태그 쓰는 사이트는 nav/.gnb 가 안 걸려서
                    // 이 fallback 이 필수. same-origin / 패턴 중복 제거는 Python 쪽에서 처리.
                    if (out.length < 5) {
                        for (const a of document.querySelectorAll('a[href]')) {
                            pushA(a);
                        }
                    }
                    return out;
                }"""
            )
            results = [a for a in (anchors or []) if a and a.get("href")]
        finally:
            context.close()
            browser.close()
    return results


def _static_anchors_via_curl(root_url: str) -> list[dict]:
    """curl_cffi(Chrome 131 impersonate) 로 HTML 받아 a[href] 파싱.

    Playwright 헤드리스가 봇 차단으로 블록당하는 사이트(사람인 등) 에서
    SSR 렌더된 네비 메뉴를 건져내는 fallback/보강.
    """
    try:
        from http_client import fetch
    except ImportError:
        try:
            from crawlers.http_client import fetch
        except ImportError:
            return []
    try:
        html = fetch(root_url, timeout=30, max_retries=1)
    except Exception as e:
        print(f"    [menu_discovery] curl_cffi fetch 실패 — {type(e).__name__}: {str(e)[:100]}")
        return []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "lxml")
    except Exception:
        return []
    out: list[dict] = []
    for a in soup.select("a[href]"):
        out.append({
            "text": a.get_text(strip=True),
            "href": a.get("href") or "",
        })
    return out


def discover_job_menus(root_url: str, timeout_ms: int = 45000) -> list[dict]:
    """사이트의 네비게이션에서 "구인/채용" 메뉴 후보 추출.

    전략 (병행):
      1. curl_cffi 로 정적 HTML fetch → SSR 메뉴 수집 (봇 차단 우회)
      2. Playwright 로 hover/스크롤 → 동적 메뉴 수집
    둘을 합친 뒤 same-origin + 패턴 중복 제거.

    Returns: [{menu_name, url, score}, ...]  score 내림차순.
    """
    parsed = urlparse(root_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"

    # 1. 정적 fetch
    anchors_static = _static_anchors_via_curl(root_url)
    if anchors_static:
        print(f"    [menu_discovery] curl_cffi 정적 수집: {len(anchors_static)}개 a 태그")

    # 2. 동적 수집 (hover 포함)
    try:
        anchors_dynamic = _extract_links_with_playwright(root_url, timeout_ms)
        if anchors_dynamic:
            print(f"    [menu_discovery] Playwright 동적 수집: {len(anchors_dynamic)}개 a 태그")
    except Exception as e:
        print(f"    [menu_discovery] Playwright 실패 — {type(e).__name__}: {str(e)[:100]}")
        anchors_dynamic = []

    anchors = anchors_static + anchors_dynamic

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
        # mailto:/tel: 같은 비-http 스킴 배제
        if ":" in href and not href.startswith("/") and not href.startswith("http"):
            continue
        full = urljoin(root_url, href).split("#")[0]
        try:
            p = urlparse(full)
        except Exception:
            continue
        # 같은 origin 만 (서브도메인 다르면 다른 사이트로 간주)
        if p.netloc and p.netloc != parsed.netloc:
            continue
        # 키워드 필터 제거 — 네비 영역의 모든 메뉴를 LLM 에게 판별 맡김.
        # (이전엔 _score_link 가 채용 키워드 없으면 버렸는데, 그러면 "중소기업" 같이
        # 키워드 없는 카테고리 메뉴는 LLM 에 도달 못 함. 사용자 요구 대비 누락.)
        key = full.rstrip("/")
        menu_name = (text[:40] or full.split("/")[-1] or "menu")
        if key in candidates:
            # 텍스트가 더 설명적이면 갱신
            if text and len(text) > len(candidates[key]["menu_name"]):
                candidates[key]["menu_name"] = menu_name
            continue
        candidates[key] = {
            "menu_name": menu_name,
            "url": full,
            "score": 1,  # 스코어는 의미 잃음 — LLM 이 판단
        }

    # 동일 URL 패턴 축약 — path 의 숫자 부분을 <id> 로 치환해서 템플릿이 같으면
    # 하나만 대표로 남김. 알바몬 `/jobs/brand/special/214..229` 같은 개별 상세 URL
    # 군이 상위를 점유하는 상황 방지.
    pattern_seen: dict[str, dict] = {}
    for c in candidates.values():
        path = urlparse(c["url"]).path
        template = re.sub(r"/\d+(?=/|$|\?)", "/<id>", path)
        key = f"{urlparse(c['url']).netloc}{template}"
        cur = pattern_seen.get(key)
        if cur is None or c["score"] > cur["score"]:
            pattern_seen[key] = c

    # 스코어 내림차순 정렬 + 상한
    ordered = sorted(pattern_seen.values(), key=lambda x: x["score"], reverse=True)
    return ordered[:MAX_MENU_CANDIDATES]
