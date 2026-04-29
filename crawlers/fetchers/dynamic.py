"""Playwright 기반 동적 fetch (SPA / JS 렌더링 사이트).

정적 fetch 와 같은 FetchResult 인터페이스. 정적이 빈 결과면 fallback 으로 사용.

비용: 헤드리스 브라우저 기동 ~1-2초 + 페이지 렌더링 ~3-5초. 정적 대비 5-10배 느림.
"""
from __future__ import annotations

import threading
from typing import Optional

from .static import FetchResult


_DEFAULT_TIMEOUT_MS = 20_000
_WAIT_NETWORK_IDLE_MS = 1_500   # network idle 후 추가 대기
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

_lock = threading.Lock()
_pw = None
_browser = None


def _ensure_browser():
    """프로세스 내 Playwright/브라우저 1회 기동, 재사용."""
    global _pw, _browser
    if _browser is not None:
        return _browser
    with _lock:
        if _browser is not None:
            return _browser
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None
        _pw = sync_playwright().start()
        _browser = _pw.chromium.launch(headless=True)
        return _browser


def shutdown() -> None:
    """프로세스 종료 시 호출 권장 (CLI 에서)."""
    global _pw, _browser
    with _lock:
        if _browser is not None:
            try:
                _browser.close()
            except Exception:  # noqa: BLE001
                pass
            _browser = None
        if _pw is not None:
            try:
                _pw.stop()
            except Exception:  # noqa: BLE001
                pass
            _pw = None


def _parse_proxy(proxy_url: str) -> Optional[dict]:
    """'http://user:pass@host:port' → Playwright proxy dict."""
    from urllib.parse import urlparse
    p = urlparse(proxy_url)
    if not p.hostname:
        return None
    out = {"server": f"{p.scheme}://{p.hostname}:{p.port or 80}"}
    if p.username:
        out["username"] = p.username
    if p.password:
        out["password"] = p.password
    return out


_BLOCKED_RESOURCE_TYPES = {"image", "font", "media", "stylesheet"}


def fetch(
    url: str,
    *,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    wait_network_idle_ms: int = _WAIT_NETWORK_IDLE_MS,
    user_agent: Optional[str] = None,
    capture_api: bool = False,
    proxy: Optional[str] = None,
    block_resources: bool = False,
) -> FetchResult:
    """capture_api=True 면 페이지 로드 동안 호출된 XHR/fetch 응답을 누적:
      - JSON 응답: result.api_calls (data 디코딩)
      - HTML 응답 (server-side rendered fragment, e.g. /advnc/ajax/getEpmtList.do):
        result.xhr_html — list URL 자동 발견에 사용."""
    browser = _ensure_browser()
    if browser is None:
        return FetchResult(url, 0, "", url, error="playwright not installed")

    ctx = None
    page = None
    api_calls: list[dict] = []
    xhr_html: list[dict] = []
    try:
        ctx_kwargs = {"user_agent": user_agent or _USER_AGENT}
        if proxy:
            pp = _parse_proxy(proxy)
            if pp:
                ctx_kwargs["proxy"] = pp
        ctx = browser.new_context(**ctx_kwargs)
        page = ctx.new_page()
        # stealth — anti-bot 감지 우회 (navigator.webdriver, canvas fingerprint 등 위장).
        # cambojob 같이 강한 anti-scraping 사이트 통과 가능성 ↑.
        try:
            from playwright_stealth import Stealth
            Stealth().apply_stealth_sync(page)
        except Exception:  # noqa: BLE001
            pass

        # 트래픽 절약 — 텍스트 추출만 필요할 때 image/font/media/stylesheet abort.
        # 프록시 풀 대역폭 한도 사이트 (use_proxy=True) 에서 60–80% 트래픽 절감.
        if block_resources:
            def _route(route):
                try:
                    if route.request.resource_type in _BLOCKED_RESOURCE_TYPES:
                        route.abort()
                    else:
                        route.continue_()
                except Exception:  # noqa: BLE001
                    try:
                        route.continue_()
                    except Exception:  # noqa: BLE001
                        pass
            page.route("**/*", _route)

        if capture_api:
            import json as _json

            def _on_response(resp):
                try:
                    rt = resp.request.resource_type
                    # xhr/fetch 만 — main document/css/img 등은 무시
                    # GET / POST 둘 다 수용 (form submit 형 AJAX 도 잡기 위해)
                    if rt not in ("xhr", "fetch"):
                        return
                    ct = (resp.headers.get("content-type") or "").lower()
                    body = resp.body()
                    if not body or len(body) < 50:
                        return
                    if "json" in ct:
                        try:
                            data = _json.loads(body)
                        except Exception:  # noqa: BLE001
                            return
                        api_calls.append({
                            "url": resp.url, "status": resp.status, "data": data,
                        })
                    elif "html" in ct or "text/plain" in ct:
                        try:
                            text = body.decode(resp.headers.get("content-encoding") or "utf-8",
                                               errors="replace")
                        except Exception:  # noqa: BLE001
                            try:
                                text = body.decode("utf-8", errors="replace")
                            except Exception:  # noqa: BLE001
                                return
                        if "<" in text:
                            xhr_html.append({
                                "url": resp.url, "status": resp.status, "text": text,
                            })
                except Exception:  # noqa: BLE001
                    pass

            page.on("response", _on_response)

        resp = page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except Exception:  # noqa: BLE001
            pass
        page.wait_for_timeout(wait_network_idle_ms)
        html = page.content()
        status = resp.status if resp else 200
        final_url = page.url
        result = FetchResult(url=url, status=status, text=html, final_url=final_url)
        if capture_api:
            # FetchResult 는 dataclass — 동적 속성 추가
            result.api_calls = api_calls  # type: ignore[attr-defined]
            result.xhr_html = xhr_html    # type: ignore[attr-defined]
        return result
    except Exception as e:  # noqa: BLE001
        return FetchResult(url, 0, "", url, error=f"{type(e).__name__}: {e}")
    finally:
        try:
            if page:
                page.close()
            if ctx:
                ctx.close()
        except Exception:  # noqa: BLE001
            pass
