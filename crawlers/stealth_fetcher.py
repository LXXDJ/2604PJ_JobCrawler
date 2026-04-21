"""
Scrapling StealthyFetcher 기반 HTML 페처.

curl_cffi 로도 막히는 사이트(DNS 해석 실패, SSL 버전 에러, SPA 빈 껍데기, JS
챌린지 등)의 폴백 경로. 실제 Chromium (Patchright) 을 띄워서 OS DNS / 완전 렌더
/ Cloudflare Turnstile 자동 풀이까지 처리.

비용:
  - 첫 호출 Patchright 초기화 ~2~4초, 페이지 로딩 5~15초
  - curl_cffi 대비 10~30배 느림 → 일반 fetch 실패 시에만 폴백

외부 API:
  fetch_html(url, timeout=45000, wait_selector=None, ...) -> str
    성공: HTML 문자열 반환
    실패: Exception (호출부가 그대로 raise 하거나 catch)
"""

from typing import Optional


_INSTALLED_CHECKED = False
_INSTALLED = False


def _check_installed() -> bool:
    """scrapling + patchright 설치 여부 확인 (한 번만 시도)."""
    global _INSTALLED_CHECKED, _INSTALLED
    if _INSTALLED_CHECKED:
        return _INSTALLED
    _INSTALLED_CHECKED = True
    try:
        from scrapling.fetchers import StealthyFetcher  # noqa: F401
        _INSTALLED = True
    except Exception as e:
        print(f"      [stealth] scrapling 미설치 또는 import 실패: {type(e).__name__}: {e}")
        _INSTALLED = False
    return _INSTALLED


def fetch_html(
    url: str,
    timeout: int = 45000,
    wait_selector: Optional[str] = None,
    wait_selector_state: str = "attached",
    solve_cloudflare: bool = True,
    real_chrome: bool = False,
    headless: bool = True,
    cookies: Optional[dict] = None,
) -> str:
    """StealthyFetcher 로 HTML 가져오기. 실패 시 예외 raise.

    solve_cloudflare: Cloudflare Turnstile 챌린지 자동 풀이. IP ban 인 사이트엔 효과
                      없지만 JS 챌린지 기반 사이트엔 유효.
    real_chrome: 시스템 Chrome 사용 (DNS/SSL 문제 회피에 유리). False 면 patchright 번들.
    wait_selector: 이 CSS 가 나올 때까지 대기 — SPA 공고 리스트 렌더 기다릴 때.
    cookies: 사전 쿠키 주입.
    """
    if not _check_installed():
        raise RuntimeError("scrapling 미설치 — pip install scrapling patchright 후 재시도")

    from scrapling.fetchers import StealthyFetcher

    kwargs = {
        "headless": headless,
        "solve_cloudflare": solve_cloudflare,
        "real_chrome": real_chrome,
        "hide_canvas": True,
        "block_webrtc": True,
        "allow_webgl": True,
        "google_search": True,
        "network_idle": True,
        "timeout": timeout,
        "locale": "ko-KR",
        "timezone_id": "Asia/Seoul",
    }
    if wait_selector:
        kwargs["wait_selector"] = wait_selector
        kwargs["wait_selector_state"] = wait_selector_state
    if cookies:
        kwargs["cookies"] = cookies

    response = StealthyFetcher.fetch(url, **kwargs)
    status = getattr(response, "status", None)
    body = getattr(response, "html_content", "") or ""

    if status and status >= 400:
        raise RuntimeError(f"stealth fetch HTTP {status} — body[:200]={body[:200]!r}")
    if len(body) < 50:
        raise RuntimeError(f"stealth fetch empty body (len={len(body)})")
    return body
