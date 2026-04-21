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
    timeout: int = 30,
    max_retries: int = 3,
    retry_backoff: float = 2.0,
    verify_ssl: bool = False,
    return_json: bool = False,
):
    """
    HTTP GET 요청 + 자동 재시도. 내부적으로 curl_cffi 의 Chrome impersonate 사용.

    Returns: response.text (기본) 또는 response.json() (return_json=True)

    실패 시 마지막 예외를 그대로 raise한다.
    (크롤링 중단이 낫지, 조용히 None 넘기면 이후 로직에서 이상 데이터 만들 수 있음)
    """
    merged_headers = dict(_DEFAULT_BROWSER_HEADERS)
    if headers:
        merged_headers.update(headers)

    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            response = cffi_requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=timeout,
                verify=verify_ssl,
                impersonate=_IMPERSONATE_TARGET,
            )
            response.raise_for_status()
            if attempt > 1:
                print(f"      [retry] {attempt}회 시도 성공")
            return response.json() if return_json else response.text

        except _requests_legacy.exceptions.HTTPError:
            # 4xx/5xx는 재시도해도 보통 같은 결과 → 즉시 중단
            raise

        except (_requests_legacy.exceptions.Timeout,
                _requests_legacy.exceptions.ConnectionError) as e:
            last_exception = e
            print(f"      [retry] {type(e).__name__} (attempt {attempt}/{max_retries})")

        except Exception as e:
            # curl_cffi 는 자체 예외(CurlError 계열)를 raise 할 수 있음 — 모두 재시도 대상.
            # HTTPError 는 status_code 기반이라 curl_cffi 도 requests.exceptions.HTTPError 로 올라옴.
            last_exception = e
            print(f"      [retry] {type(e).__name__}: {e} (attempt {attempt}/{max_retries})")

        if attempt < max_retries:
            time.sleep(retry_backoff * attempt)

    raise last_exception
