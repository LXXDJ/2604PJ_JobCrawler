"""curl_cffi 기반 정적 fetch (브라우저 핑거프린트 흉내)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None  # type: ignore


DEFAULT_TIMEOUT = 20
DEFAULT_IMPERSONATE = "chrome124"


@dataclass
class FetchResult:
    url: str
    status: int
    text: str
    final_url: str
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None and 200 <= self.status < 400


def fetch(
    url: str,
    *,
    timeout: int = DEFAULT_TIMEOUT,
    impersonate: str = DEFAULT_IMPERSONATE,
    headers: Optional[dict[str, str]] = None,
    session: Optional["cffi_requests.Session"] = None,
) -> FetchResult:
    """단일 GET. session 이 주어지면 그 세션으로 호출 (cookie/session 유지).
    cookie 가 필요한 anti-scraping 사이트 (cambojob 류) 대응.
    """
    if cffi_requests is None:
        return FetchResult(url, 0, "", url, error="curl_cffi not installed")

    try:
        if session is not None:
            resp = session.get(
                url, timeout=timeout, headers=headers or {}, allow_redirects=True,
            )
        else:
            resp = cffi_requests.get(
                url, timeout=timeout, impersonate=impersonate,
                headers=headers or {}, allow_redirects=True,
            )
        return FetchResult(
            url=url,
            status=resp.status_code,
            text=resp.text or "",
            final_url=str(resp.url),
        )
    except Exception as e:  # noqa: BLE001
        return FetchResult(url, 0, "", url, error=f"{type(e).__name__}: {e}")


def make_session(impersonate: str = DEFAULT_IMPERSONATE):
    """curl_cffi Session 생성 — 같은 host 의 연속 fetch 시 cookie 유지."""
    if cffi_requests is None:
        return None
    return cffi_requests.Session(impersonate=impersonate)
