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
) -> FetchResult:
    if cffi_requests is None:
        return FetchResult(url, 0, "", url, error="curl_cffi not installed")

    try:
        resp = cffi_requests.get(
            url,
            timeout=timeout,
            impersonate=impersonate,
            headers=headers or {},
            allow_redirects=True,
        )
        return FetchResult(
            url=url,
            status=resp.status_code,
            text=resp.text or "",
            final_url=str(resp.url),
        )
    except Exception as e:  # noqa: BLE001
        return FetchResult(url, 0, "", url, error=f"{type(e).__name__}: {e}")
