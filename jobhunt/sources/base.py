"""사이트 어댑터 공통 — 리스트 항목 형식 + 재시도 GET."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field

from curl_cffi import requests


@dataclass
class ListItem:
    external_id: str
    url: str
    title: str
    company: str
    company_id: str | None = None
    company_industry: str | None = None
    location: str | None = None
    years_from: int | None = None
    years_to: int | None = None      # 100 = 무관 (원티드 표기에 맞춤)
    due_date: str | None = None      # YYYY-MM-DD, None = 상시
    category_ids: list = field(default_factory=list)   # 사이트 직무 분류 (원티드 int, 나머지는 문자열)
    ai_category: bool = False        # 사이트 직무 분류상 AI/ML 직무인가 (1차 필터에서 제목 키워드 없이도 통과)


def parse_krw_eok(text: str | None) -> float | None:
    """'264억 3,537만원' → 264.3537, '46억 3천만원' → 46.3, '1조 2,000억원' → 12000, '-20억 4천만원' → -20.4 (억원)."""
    t = (text or "").replace(",", "").replace(" ", "")
    m = re.match(r"(-)?(?:(\d+(?:\.\d+)?)조)?(?:(\d+(?:\.\d+)?)억)?(.*?)(만)?원?$", t)
    if not m or not (m.group(2) or m.group(3) or m.group(4)):
        return None
    man = 0.0
    rest = m.group(4) or ""
    if rest.isdigit():
        man = float(rest)
    else:
        for num, unit in re.findall(r"(\d+)([천백십]?)", rest):
            man += int(num) * {"천": 1000, "백": 100, "십": 10, "": 1}[unit]
    if not m.group(5) and not m.group(2) and not m.group(3):
        man /= 10000          # 단위가 그냥 '원' 이면 만원 단위가 아님
    total = float(m.group(2) or 0) * 10000 + float(m.group(3) or 0) + man / 10000
    return -total if m.group(1) else total


# 403 도 재시도 — 연속 요청 시 일시 차단됐다가 잠시 후 풀림 (원티드 2026-10-30 확인)
RETRY_STATUS = (403, 429, 500, 502, 503, 504)


def get(s: requests.Session, url: str, *, params: dict | None = None, headers: dict | None = None,
        retries: int = 4) -> requests.Response:
    for attempt in range(retries):
        try:
            r = s.get(url, params=params, headers=headers, timeout=30)
            if r.status_code == 200:
                return r
            if r.status_code not in RETRY_STATUS or attempt == retries - 1:
                r.raise_for_status()
        except requests.RequestsError:
            if attempt == retries - 1:
                raise
        time.sleep(5 * 2 ** attempt)
    raise RuntimeError(f"retries exhausted for {url}")
