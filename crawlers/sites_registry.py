"""
동적으로 등록된 사이트들의 config 저장소 (data/sites.json 기반).

main.py 의 REGISTERED_CRAWLS 가 하드코딩된 기본 사이트라면,
이 모듈은 `python main.py add <URL>` 로 추가된 사이트들을 관리한다.

신 스키마 (Phase 1):
[
  {
    "site_id": "hanin",
    "url": "http://www.hanin.or.kr/bbs/board.php?bo_table=Information",
    "site_type": "gnuboard",                # 분류 (진단)
    "added_at": "2026-04-17T...",
    "extraction_method": "dom",             # 로직 선택
    "requires_render": false,
    "source": {...},                        # extraction_method 별 스키마
    "pagination": {...},
    "validated": true,
    "validation_report": {...}
  },
  ...
]

레거시 스키마 (구버전, 마이그레이션 대상):
  - "crawler": "gnuboard_crawler" / "camhr_crawler"
  - "config": {...flat...}
    레거시 엔트리도 dispatcher 가 호환 라우팅하므로 시스템은 계속 동작.
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlparse, urlunparse


# extraction_method → Phase 1 에서 매핑된 크롤러 모듈명
# dispatcher 가 실제 import 하므로 여기선 "등록 가능한 방법 목록" 역할만.
EXTRACTION_METHOD_TO_CRAWLER = {
    "dom": "dom_crawler",
    # "api":           "api_crawler",       # Phase 3
    # "embedded_json": "embedded_crawler",  # Phase 2
}


# SiteType → ExtractionMethod 추론 (Phase 1 제한된 매핑)
# analyzer 가 extraction_method 를 직접 반환하도록 바뀌기 전까지 임시 사용.
SITE_TYPE_TO_EXTRACTION_METHOD = {
    "gnuboard": "dom",
    "static_html": "dom",
    "wordpress": "dom",
    "api_discovered": "api",  # crawler 미구현 — can_register 가 거부
    # spa_* 는 Phase 2 에서 embedded_json 경로 뚫리면 채움
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
    URL 에서 site_id 자동 추출.
      siemreap.korean.net  → siemreap
      www.hanin.or.kr      → hanin
    """
    host = urlparse(url).netloc
    if host.startswith("www."):
        host = host[4:]
    return host.split(".")[0] or "site"


def resolve_unique_site_id(desired: str, taken: set) -> str:
    """site_id 충돌 회피: -2, -3 식으로 붙임."""
    if desired not in taken:
        return desired
    i = 2
    while f"{desired}-{i}" in taken:
        i += 1
    return f"{desired}-{i}"


# ============================================================
# 중복 감지 (신·구 스키마 공통)
# ============================================================

def normalize_url(url: str) -> str:
    return url.rstrip("/").lower()


def _entry_base_url(entry: dict) -> str:
    """신·구 스키마 어느 쪽이든 base_url 추출."""
    # 신 스키마: entry["source"]["base_url"]
    source = entry.get("source")
    if isinstance(source, dict) and source.get("base_url"):
        return source["base_url"]
    # 구 스키마: entry["config"]["base_url"]
    legacy_config = entry.get("config") or {}
    return legacy_config.get("base_url", "")


def _entry_board_table(entry: dict) -> Optional[str]:
    """gnuboard 구분자 (신·구 스키마 공통). 없으면 None."""
    # 신 스키마: entry["source"]["list_params"]["bo_table"]
    source = entry.get("source")
    if isinstance(source, dict):
        params = source.get("list_params") or {}
        if "bo_table" in params:
            return params["bo_table"]
    # 구 스키마: entry["config"]["board_table"]
    legacy_config = entry.get("config") or {}
    return legacy_config.get("board_table")


def is_same_site(entry_a: dict, entry_b: dict) -> bool:
    """
    두 엔트리가 같은 사이트/게시판을 가리키는지.
    신·구 스키마 혼재 상황에서도 동작.
    """
    a_base = normalize_url(_entry_base_url(entry_a))
    b_base = normalize_url(_entry_base_url(entry_b))
    if not a_base or not b_base or a_base != b_base:
        return False

    a_board = _entry_board_table(entry_a)
    b_board = _entry_board_table(entry_b)
    if a_board is not None or b_board is not None:
        return a_board == b_board
    return True


def find_duplicate(candidate_entry: dict, entries: list) -> Optional[dict]:
    """같은 사이트/게시판을 가리키는 기존 엔트리 반환 (없으면 None)."""
    for entry in entries:
        if is_same_site(candidate_entry, entry):
            return entry
    return None


# ============================================================
# AnalysisResult → 신 스키마 config 변환
# ============================================================

def _gnuboard_analysis_to_source(result, url: str) -> dict:
    """gnuboard 분석 결과 → source 블록.

    analyzer 가 내는 config:
        {platform, base_url, bbs_url, board_table, theme, selectors, parse_mode}
    """
    c = result.config
    base_url = c.get("base_url", "")
    return {
        "list_url": f"{base_url}/bbs/board.php",
        "list_params": {"bo_table": c.get("board_table", "")},
        "base_url": base_url,
        "selectors": c.get("selectors") or {},
        "parse_mode": c.get("parse_mode", "direct"),
        "skip_row_if_has_class": ["fz_list_th", "na-table-head"],
        "external_id_from_url_param": "wr_id",
        "theme": c.get("theme", ""),  # 진단용 (크롤러는 안 씀)
    }


def _static_html_analysis_to_source(result, url: str) -> dict:
    """static_html 분석 결과 → source 블록.

    LLM 이 낸 config 는 대체로:
        {platform, base_url, board_table, theme, parse_mode, selectors}
    list_url 은 원본 URL 에서 page 쿼리만 떼어내 사용한다.
    """
    parsed = urlparse(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    list_params = {k: v for k, v in query_pairs if k.lower() != "page"}
    list_url = urlunparse(parsed._replace(query=""))

    c = result.config
    return {
        "list_url": list_url,
        "list_params": list_params,
        "base_url": c.get("base_url") or f"{parsed.scheme}://{parsed.netloc}",
        "selectors": c.get("selectors") or {},
        "parse_mode": c.get("parse_mode", "direct"),
        # 정적 사이트는 헤더 행 필터 기본값 없음 (selectors 가 충분히 구체적이길 기대)
        "skip_row_if_has_class": [],
    }


def wrap_flat_config_as_entry(flat_config: dict) -> dict:
    """
    레거시 flat config (평탄한 dict) 를 entry 모양으로 감싼다.
    is_same_site / find_duplicate 가 entry 를 기대하므로, cmd_add 에서 result.config 를
    넣기 전에 이걸 거쳐야 한다.
    """
    return {"config": flat_config}


def analysis_to_new_schema_config(result, url: str) -> dict:
    """
    AnalysisResult 를 신 스키마 엔트리 본문 (extraction_method/source/pagination 포함) 로 변환.
    지원하지 않는 site_type 은 ValueError.
    """
    site_type = result.site_type.value
    method = SITE_TYPE_TO_EXTRACTION_METHOD.get(site_type)
    if method is None:
        raise ValueError(f"site_type={site_type!r} 에 대한 extraction_method 추론 매핑 없음")

    if method != "dom":
        raise ValueError(
            f"extraction_method={method!r} 은 Phase 1 범위 밖 — 등록 불가 "
            f"(site_type={site_type})"
        )

    if site_type == "gnuboard":
        source = _gnuboard_analysis_to_source(result, url)
    elif site_type == "static_html":
        source = _static_html_analysis_to_source(result, url)
    else:
        # wordpress 등 DOM 이지만 변환 템플릿 아직 없음 → Phase 에서 추가
        raise ValueError(
            f"site_type={site_type!r} 변환 템플릿 미구현 (Phase 1 에선 gnuboard/static_html 만)"
        )

    return {
        "extraction_method": "dom",
        "requires_render": False,
        "source": source,
        "pagination": {"type": "url_param", "param": "page", "start": 1},
    }


# ============================================================
# 등록 가능성 판정 (1차 구조 체크. validator 는 main.py cmd_add 에서 별도 호출)
# ============================================================

def can_register(analysis_result) -> tuple[bool, str]:
    """
    AnalysisResult 가 구조적으로 등록 가능한지 1차 판정.

    실제 검증(selectors 가 HTML 에서 매칭되는지 등)은 main.py cmd_add 에서
    validator 호출로 수행. 이 함수는 "변환 자체가 가능한가" 까지만 본다.
    """
    # 1. 분석 자체가 유효한가
    if not analysis_result.is_valid(0.5):
        return False, (
            f"신뢰도 부족 또는 타입 미상 "
            f"(site_type={analysis_result.site_type.value}, "
            f"confidence={analysis_result.confidence:.2f})"
        )

    config = analysis_result.config
    site_type = analysis_result.site_type.value

    # 2. API 자동 발견됐지만 Phase 3 까지 api_crawler 미구현
    if site_type == "api_discovered":
        endpoint = config.get("api_endpoint", "?")
        return False, (
            f"API 엔드포인트 자동 발견: {endpoint}\n"
            f"      → extraction_method='api' 크롤러는 Phase 3 에서 구현 예정.\n"
            f"      → 당분간 crawlers/hardcoded_crawls.py 에 수동 어댑터 추가 필요."
        )

    # 3. SPA 감지됐는데 Playwright 도 실패
    if config.get("needs_playwright_discovery"):
        return False, "SPA 사이트 — Playwright 자동 발견이 후보 API 를 찾지 못함"

    # 4. site_type → extraction_method 매핑 가능?
    method = SITE_TYPE_TO_EXTRACTION_METHOD.get(site_type)
    if method is None:
        return False, f"site_type={site_type!r} 은 아직 어떤 extraction_method 에도 연결 안 됨"

    # 5. extraction_method 에 해당하는 크롤러가 Phase 1 에서 구현됐나
    if method not in EXTRACTION_METHOD_TO_CRAWLER:
        return False, (
            f"extraction_method={method!r} 크롤러가 이 Phase 에서 구현 안 됨"
        )

    # 6. DOM 에 한해 selectors 최소 조건
    if method == "dom":
        selectors = config.get("selectors") or {}
        if not selectors.get("list_rows") or not selectors.get("subject_link"):
            return False, "selectors.list_rows / subject_link 비어있음 (필수)"

    return True, ""


# ============================================================
# 엔트리 생성 (신 스키마)
# ============================================================

def build_entry(analysis_result, site_id: str, validation_report: Optional[dict] = None) -> dict:
    """
    AnalysisResult → 신 스키마 sites.json 엔트리.

    validation_report 는 validator.validate_*_config(...).to_dict() 결과.
    None 이면 validated=false (저장해도 되지만 dispatcher 는 여전히 돌림 — 진단 추적용).
    """
    new_config = analysis_to_new_schema_config(analysis_result, analysis_result.url)
    entry = {
        "site_id": site_id,
        "url": analysis_result.url,
        "site_type": analysis_result.site_type.value,
        "added_at": datetime.now(timezone.utc).isoformat(),
        **new_config,
        "validated": bool(validation_report and validation_report.get("ok")),
        "validation_report": validation_report or {},
    }
    return entry
