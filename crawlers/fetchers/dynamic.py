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


def fetch(
    url: str,
    *,
    timeout_ms: int = _DEFAULT_TIMEOUT_MS,
    wait_network_idle_ms: int = _WAIT_NETWORK_IDLE_MS,
    user_agent: Optional[str] = None,
    capture_api: bool = False,
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
        ctx = browser.new_context(user_agent=user_agent or _USER_AGENT)
        page = ctx.new_page()

        if capture_api:
            import json as _json

            def _on_response(resp):
                try:
                    if resp.request.resource_type not in ("xhr", "fetch"):
                        return
                    if resp.request.method != "GET":
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
