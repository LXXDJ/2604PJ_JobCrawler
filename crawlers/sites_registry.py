"""
동적으로 등록된 사이트들의 config 저장소 (data/sites.json 기반).

main.py의 REGISTERED_CRAWLS가 하드코딩된 기본 사이트라면,
이 모듈은 `python main.py add <URL>` 로 추가된 사이트들을 관리한다.

파일 포맷 (data/sites.json):
[
  {
    "site_id": "siemreap",
    "crawler": "gnuboard_crawler",
    "url": "https://siemreap.korean.net/...",
    "site_type": "gnuboard",
    "added_at": "2026-04-17T...",
    "config": { ... analyzer가 생성한 크롤러 설정 ... }
  },
  ...
]
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse


# SiteType(value) → 크롤러 모듈 이름
# 여기 없는 타입은 등록 거부됨 (크롤러 미구현 상태)
SITE_TYPE_TO_CRAWLER = {
    "gnuboard": "gnuboard_crawler",
    # spa_nuxt, wordpress 등은 아직 범용 크롤러 없음
}


# ============================================================
# 파일 입출력
# ============================================================

def load_all(path: str) -> list:
    """sites.json 로드 (파일 없으면 빈 리스트)"""
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_all(path: str, entries: list) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)


# ============================================================
# site_id 자동 생성
# ============================================================

def extract_site_id(url: str) -> str:
    """
    URL에서 site_id 자동 추출.
      siemreap.korean.net  → siemreap
      www.hanin.or.kr      → hanin
      api.camhr.com        → api   (의도치 않을 수 있음 — 수동 지정 고려)
    """
    host = urlparse(url).netloc
    if host.startswith("www."):
        host = host[4:]
    return host.split(".")[0] or "site"


def resolve_unique_site_id(desired: str, taken: set) -> str:
    """site_id가 이미 쓰이고 있으면 -2, -3 식으로 붙여서 충돌 회피"""
    if desired not in taken:
        return desired
    i = 2
    while f"{desired}-{i}" in taken:
        i += 1
    return f"{desired}-{i}"


# ============================================================
# 중복/정규화
# ============================================================

def normalize_url(url: str) -> str:
    """끝 슬래시 제거 + 소문자화"""
    return url.rstrip("/").lower()


def is_same_site(config_a: dict, config_b: dict) -> bool:
    """
    두 config가 같은 사이트(또는 같은 보드)를 가리키는지.

    - base_url이 다르면 → 다른 사이트
    - 그누보드처럼 board_table이 있으면 그것까지 같아야 동일로 본다
      (동일 사이트 다른 게시판은 중복 아님, 각각 별개 대상)
    """
    a_base = normalize_url(config_a.get("base_url", ""))
    b_base = normalize_url(config_b.get("base_url", ""))
    if not a_base or not b_base or a_base != b_base:
        return False

    a_board = config_a.get("board_table")
    b_board = config_b.get("board_table")
    if a_board is not None or b_board is not None:
        return a_board == b_board
    return True


def find_duplicate(candidate_config: dict, entries: list) -> Optional[dict]:
    """
    동일 사이트/보드를 가리키는 기존 엔트리 탐색.
    REGISTERED_CRAWLS나 sites.json 어느 쪽에든 쓸 수 있음 (둘 다 동일 shape).
    """
    for entry in entries:
        if is_same_site(candidate_config, entry.get("config", {})):
            return entry
    return None


# ============================================================
# 등록 가능성 판정
# ============================================================

def can_register(analysis_result) -> tuple[bool, str]:
    """
    AnalysisResult가 등록 가능한지 판단.

    거부 조건:
      1. confidence 부족 or 타입 미상 (is_valid=False)
      2. api_discovered — Playwright 로 API 를 찾았지만 자동 크롤링은 미구현.
         사람이 hardcoded_crawls.py 에 어댑터를 짜야 함. (콘솔에 후보 API 출력)
      3. needs_playwright_discovery: True  (SPA 인데 Playwright 도 실패한 경우)
      4. 해당 site_type용 크롤러 미구현
      5. gnuboard인데 selectors 비어있음 (알려지지 않은 테마)

    Returns: (ok, reason)
    """
    # 1.
    if not analysis_result.is_valid(0.5):
        return False, (
            f"신뢰도 부족 또는 타입 미상 "
            f"(site_type={analysis_result.site_type.value}, "
            f"confidence={analysis_result.confidence:.2f})"
        )

    config = analysis_result.config
    site_type_value = analysis_result.site_type.value

    # 2.
    if site_type_value == "api_discovered":
        endpoint = config.get("api_endpoint", "?")
        return False, (
            f"API 엔드포인트를 자동 발견함: {endpoint}\n"
            f"      → 자동 크롤링은 미구현. crawlers/hardcoded_crawls.py 에 어댑터 추가 필요.\n"
            f"      → 상위 후보 / 응답 샘플은 아래 config 참조."
        )

    # 3.
    if config.get("needs_playwright_discovery"):
        return False, (
            "SPA 사이트 — Playwright 자동 발견이 후보 API 를 찾지 못함 (수동 분석 필요)"
        )

    # 4.
    if site_type_value not in SITE_TYPE_TO_CRAWLER:
        return False, f"사이트 타입 '{site_type_value}'용 범용 크롤러가 아직 없음"

    # 5.
    if site_type_value == "gnuboard" and not config.get("selectors"):
        return False, "selectors 비어있음 — 알려지지 않은 그누보드 테마 (수동 지정 필요)"

    return True, ""


# ============================================================
# 엔트리 생성
# ============================================================

def build_entry(analysis_result, site_id: str) -> dict:
    """AnalysisResult → sites.json 엔트리 포맷 변환"""
    crawler_name = SITE_TYPE_TO_CRAWLER[analysis_result.site_type.value]
    return {
        "site_id": site_id,
        "crawler": crawler_name,
        "url": analysis_result.url,
        "site_type": analysis_result.site_type.value,
        "added_at": datetime.now(timezone.utc).isoformat(),
        "config": analysis_result.config,
    }
