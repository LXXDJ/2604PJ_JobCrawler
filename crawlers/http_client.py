"""
공통 HTTP 요청 유틸리티

모든 크롤러와 분석기가 같은 재시도 로직을 사용하도록 중앙화.
main.py의 HTTP_TIMEOUT/HTTP_MAX_RETRIES/HTTP_RETRY_BACKOFF 설정을 그대로 받아쓴다.
"""

import time
import requests
from typing import Optional


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


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
    HTTP GET 요청 + 자동 재시도.

    Returns: response.text (기본) 또는 response.json() (return_json=True)

    실패 시 마지막 예외를 그대로 raise한다.
    (크롤링 중단이 낫지, 조용히 None 넘기면 이후 로직에서 이상 데이터 만들 수 있음)
    """
    merged_headers = {"User-Agent": USER_AGENT}
    if headers:
        merged_headers.update(headers)

    last_exception = None

    for attempt in range(1, max_retries + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers=merged_headers,
                timeout=timeout,
                verify=verify_ssl,
            )
            response.raise_for_status()
            if attempt > 1:
                print(f"      [retry] {attempt}회 시도 성공")
            return response.json() if return_json else response.text

        except requests.exceptions.HTTPError as e:
            # 4xx/5xx는 재시도해도 보통 같은 결과 → 즉시 중단
            raise

        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exception = e
            print(f"      [retry] {type(e).__name__} (attempt {attempt}/{max_retries})")

        except Exception as e:
            last_exception = e
            print(f"      [retry] {type(e).__name__}: {e} (attempt {attempt}/{max_retries})")

        if attempt < max_retries:
            time.sleep(retry_backoff * attempt)

    raise last_exception
