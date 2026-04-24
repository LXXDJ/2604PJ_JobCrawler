"""
공통 HTTP 요청 유틸리티

모든 크롤러와 분석기가 같은 재시도 로직을 사용하도록 중앙화.
main.py의 HTTP_TIMEOUT/HTTP_MAX_RETRIES/HTTP_RETRY_BACKOFF 설정을 그대로 받아쓴다.

봇차단 우회: curl_cffi 로 실제 Chrome 의 TLS / JA3 / HTTP2 프로파일을 흉내낸다.
requests 는 사람인·캐치·잡플래닛·하이브레인 같은 사이트에서 403 으로 거절당함
(UA / Accept 헤더만 위장해도 JA3 지문으로 봇 판별당하기 때문).
"""

import time
from typing import Optional

import requests as _requests_legacy  # 예외 타입만 사용
from curl_cffi import requests as cffi_requests

try:
    from proxy_pool import GLOBAL as _PROXY_POOL
except ImportError:
    from crawlers.proxy_pool import GLOBAL as _PROXY_POOL


# 실제 Chrome 131 의 JA3/HTTP2 프로파일을 그대로 흉내낸다.
# curl_cffi 가 지원하는 target 중 가장 최신 안정판.
_IMPERSONATE_TARGET = "chrome131"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# curl_cffi 가 impersonate 시 Accept/Accept-Language/Sec-Fetch-* 등은 자동 주입하지만,
# 명시하면 일관성이 좋고 API 호출(referer 없이 JSON) 같이 분기된 경우에도 잘 동작.
_DEFAULT_BROWSER_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}


def fetch(
    url: str,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    cookies: Optional[dict] = None,
    timeout: int = 30,
    max_retries: int = 3,
    retry_backoff: float = 2.0,
    verify_ssl: bool = False,
    return_json: bool = False,
    cf_bypass_on_403: bool = False,
    use_stealth_on_fail: bool = False,
):
    """
    HTTP GET 요청 + 자동 재시도. 내부적으로 curl_cffi 의 Chrome impersonate 사용.

    cookies: 사전 주입할 쿠키 dict. Cloudflare 챌린지 통과 후 cf_clearance 같은
             쿠키를 넘길 때 사용.
    cf_bypass_on_403: True 면 첫 403 응답시 Playwright 로 쿠키 워밍업 후 재시도 1회.
                     리멤버·자소설 같은 Cloudflare WAF 뒤 사이트에서 유효.
    use_stealth_on_fail: True 면 curl_cffi 완전 실패 (DNS/SSL/Timeout/403 모두) 시
                     최후 폴백으로 scrapling StealthyFetcher 로 HTML 재수집.
                     LG careers.lge.com (DNS), 멀티잡 (SSL), 캐치 (SPA) 등 대응.
                     return_json=True 일 땐 무시 (JSON API 는 브라우저 경유 무의미).

    Returns: response.text (기본) 또는 response.json() (return_json=True)

    실패 시 마지막 예외를 그대로 raise한다.
    """
    merged_headers = dict(_DEFAULT_BROWSER_HEADERS)
    if headers:
        merged_headers.update(headers)

    last_exception = None
    cf_bypass_attempted = False

    # proxy 풀 활성화면 매 attempt 마다 다른 proxy 시도. 403 받은 proxy 는 cool-down.
    tried_proxies: set[str] = set()

    for attempt in range(1, max_retries + 1):
        proxy_obj = _PROXY_POOL.pick(exclude=tried_proxies) if _PROXY_POOL else None
        proxies_arg = None
        if proxy_obj:
            proxy_url = _PROXY_POOL.to_url(proxy_obj)
            proxies_arg = {"http": proxy_url, "https": proxy_url}
            tried_proxies.add(f"{proxy_obj['host']}:{proxy_obj['port']}")

        try:
            response = cffi_requests.get(
                url,
                params=params,
                headers=merged_headers,
                cookies=cookies,
                timeout=timeout,
                verify=verify_ssl,
                impersonate=_IMPERSONATE_TARGET,
                proxies=proxies_arg,
            )
            response.raise_for_status()
            if attempt > 1:
                print(f"      [retry] {attempt}회 시도 성공")
            return response.json() if return_json else response.text

        except Exception as e:
            # curl_cffi 는 HTTPError/CurlError 등을 자체 네임스페이스로 raise.
            # _requests_legacy.HTTPError 에 상속되지 않아 except 로 따로 못 잡음
            # → 통합해서 status_code 속성 유무로 분기.
            status = getattr(getattr(e, "response", None), "status_code", None)
            is_http_error = status is not None

            # 403 받은 proxy 는 cool-down 으로 빼두기
            if status == 403 and proxy_obj:
                _PROXY_POOL.mark_failed(proxy_obj)
                # 풀에 다른 proxy 가 남아있으면 즉시 다른 IP 로 재시도
                remaining = len(_PROXY_POOL) - len(tried_proxies)
                if remaining > 0 and attempt < max_retries:
                    print(f"      [proxy] 403 — {proxy_obj['host']} cool-down, 다른 IP 로 재시도 ({remaining}개 남음)")
                    continue

            if is_http_error:
                # 403 + CF bypass 옵션 켜졌으면 Playwright 쿠키 워밍업 한 번 시도
                if (
                    status == 403
                    and cf_bypass_on_403
                    and not cf_bypass_attempted
                ):
                    cf_bypass_attempted = True
                    try:
                        from cf_bypass import warm_cookies
                    except ImportError:
                        from crawlers.cf_bypass import warm_cookies
                    print(f"      [CF bypass] 403 감지 — Playwright 로 쿠키 워밍업")
                    warmed = warm_cookies(url, timeout_ms=timeout * 1000)
                    if warmed:
                        cookies = {**(cookies or {}), **warmed}
                        print(f"      [CF bypass] 쿠키 {len(warmed)}개 획득 — 재시도")
                        continue
                    print(f"      [CF bypass] 워밍업 실패")
                # HTTP 에러지만 use_stealth_on_fail=True 면 StealthyFetcher 폴백
                # (403 외에도 404/500 등에서 SPA 가 실제로는 렌더되는 경우가 있음)
                if use_stealth_on_fail and not return_json:
                    try:
                        from stealth_fetcher import fetch_html as _stealth_fetch
                    except ImportError:
                        from crawlers.stealth_fetcher import fetch_html as _stealth_fetch
                    try:
                        print(f"      [stealth] HTTP {status} — StealthyFetcher 폴백 시도")
                        html = _stealth_fetch(
                            url,
                            timeout=max(timeout * 1000, 30000),
                            cookies=cookies,
                        )
                        print(f"      [stealth] 성공 (body {len(html)}자)")
                        return html
                    except Exception as se:
                        print(f"      [stealth] 폴백 실패: {type(se).__name__}: {str(se)[:150]}")
                # 일반 4xx/5xx 는 재시도해도 같은 결과 → 즉시 중단
                raise

            # Timeout/ConnectionError/CurlError — 재시도 대상
            last_exception = e
            print(f"      [retry] {type(e).__name__}: {str(e)[:100]} (attempt {attempt}/{max_retries})")

        if attempt < max_retries:
            time.sleep(retry_backoff * attempt)

    # 마지막 폴백 — use_stealth_on_fail=True 면 StealthyFetcher (Chromium) 로 한 번 더.
    # DNS/SSL/Timeout 같은 curl_cffi 한계 우회 + SPA 완전 렌더까지 한 번에.
    # JSON API 는 제외 — 브라우저 경유한다고 JSON 파싱이 달라지지 않음.
    if use_stealth_on_fail and not return_json:
        try:
            from stealth_fetcher import fetch_html as _stealth_fetch
        except ImportError:
            from crawlers.stealth_fetcher import fetch_html as _stealth_fetch
        try:
            print(f"      [stealth] curl_cffi 실패 — StealthyFetcher 폴백 시도")
            html = _stealth_fetch(
                url,
                timeout=max(timeout * 1000, 30000),
                cookies=cookies,
            )
            print(f"      [stealth] 성공 (body {len(html)}자)")
            return html
        except Exception as e:
            print(f"      [stealth] 폴백 실패: {type(e).__name__}: {str(e)[:150]}")

    raise last_exception
