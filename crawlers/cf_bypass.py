"""
Cloudflare / WAF 쿠키 워밍업 모듈.

curl_cffi 의 Chrome TLS impersonate 로도 뚫리지 않는 사이트 (리멤버·자소설닷컴·
슈퍼루키 류) 는 보통 Cloudflare 의 JS 챌린지를 통과해야 한다. 챌린지를 풀면
`cf_clearance` / `__cf_bm` 등 쿠키가 발급되고, 이후 같은 쿠키로 요청하면 통과됨.

전략:
  1. Playwright 로 실제 브라우저를 띄워 URL 방문 (stealth tweak 포함)
  2. JS 챌린지가 자동 실행되고 쿠키 발급
  3. 브라우저 context 에서 쿠키 추출
  4. curl_cffi 에게 같은 쿠키를 넘겨서 평문 요청

비용:
  - Playwright 기동 ~3~5초, 챌린지 대기 ~3~5초 → 총 6~10초
  - 쿠키는 도메인별로 5분 캐시 (_CACHE_TTL_SEC) — 같은 도메인 다음 호출은 즉시

외부 API:
  warm_cookies(url, timeout_ms=30000) -> dict[str, str]
"""

import time
from typing import Optional
from urllib.parse import urlparse


# 도메인별 워밍업 결과 캐시. 같은 세션에서 rememberapp 여러 번 접근해도 Playwright
# 다시 안 띄움. TTL 지나면 만료 — cf_clearance 는 보통 30분 유효하지만 보수적으로 5분.
_CACHE: dict = {}
_CACHE_TTL_SEC = 300

# Playwright webdriver 탐지 회피용 스니펫. 완벽한 stealth 는 아니지만 기본 방어 통과.
_STEALTH_INIT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
Object.defineProperty(navigator, 'languages', {get: () => ['ko-KR', 'ko', 'en-US', 'en']});
window.chrome = {runtime: {}};
"""

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)


def warm_cookies(url: str, timeout_ms: int = 30000) -> dict:
    """URL 에 Playwright 로 접속해 Cloudflare 챌린지 통과 후 쿠키 반환.

    도메인별 5분 캐시 — 같은 도메인 재호출 시 Playwright 재기동 없이 캐시 반환.

    Returns: {cookie_name: value} dict. 실패 시 빈 dict.
    """
    domain = urlparse(url).netloc
    now = time.time()

    cached = _CACHE.get(domain)
    if cached and now < cached[1]:
        return cached[0]

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {}

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                    "--no-sandbox",
                ],
            )
            try:
                context = browser.new_context(
                    user_agent=_USER_AGENT,
                    viewport={"width": 1920, "height": 1080},
                    locale="ko-KR",
                    ignore_https_errors=True,
                )
                page = context.new_page()
                page.add_init_script(_STEALTH_INIT)
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                except Exception:
                    # 챌린지 중 redirect 되거나 timeout 이어도 쿠키는 받았을 수 있음
                    pass
                # 챌린지가 JS 로 자동 진행되는 시간 확보 (5초 경험적으로 충분)
                page.wait_for_timeout(5000)
                raw = context.cookies()
            finally:
                browser.close()
    except Exception:
        return {}

    cookies = {c["name"]: c["value"] for c in raw if c.get("name") and c.get("value")}
    _CACHE[domain] = (cookies, now + _CACHE_TTL_SEC)
    return cookies


def clear_cache(domain: Optional[str] = None) -> None:
    """디버그 용 캐시 초기화. domain 지정 시 그것만, 없으면 전체."""
    if domain:
        _CACHE.pop(domain, None)
    else:
        _CACHE.clear()
