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


# extraction_method → 등록된 크롤러 모듈명
# dispatcher 가 실제 import 하므로 여기선 "등록 가능한 방법 목록" 역할만.
EXTRACTION_METHOD_TO_CRAWLER = {
    "dom": "dom_crawler",
    "embedded_json": "embedded_crawler",  # Phase 2
    "api": "api_crawler",                 # Phase 3
}


# API 응답에서 "페이지 번호 파라미터" 후보 — list_params 에서 발견되면 그걸 pagination.param 으로.
API_PAGE_PARAM_CANDIDATES = ["page", "pageNo", "pageNum", "pageNumber", "p"]


# SiteType → ExtractionMethod 추론.
# analyzer 가 extraction_method 를 직접 반환하도록 바뀌기 전까지 임시 사용.
SITE_TYPE_TO_EXTRACTION_METHOD = {
    "gnuboard": "dom",
    "static_html": "dom",
    "wordpress": "dom",
    "api_discovered": "api",                          # crawler 미구현 — can_register 가 거부
    "embedded_json_discovered": "embedded_json",      # Phase 2
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

# 의미 없는 서브도메인 접두어 — host 의 맨 앞에 있으면 스킵하고 그 다음 토큰을 site_id 로.
# job.incruit.com → "job" 이 아닌 "incruit" 가 되어야 함. "www" 만 벗기던 기존 로직의 보강.
# 공격적으로 넓히면 정상 사이트가 엉뚱한 id 로 저장될 수 있으므로, 흔히 쓰이는 prefix 만.
_SUBDOMAIN_SKIP_PREFIXES = {
    "www", "www2", "ww",
    "api", "job", "jobs", "recruit", "recruits", "career", "careers",
    "shop", "m", "mobile",
    "en", "ko", "ja", "zh", "fr",
    "admin", "my", "user", "auth",
    "cdn", "static", "media", "img", "assets",
    "mail", "smtp", "blog",
}

# 2단계 ccTLD — 도메인 끝에 붙으면 통째로 잘라내야 함.
# 안 그러면 job.career.co.kr → "co" 같은 잘못된 site_id 가 나옴.
_CCTLD_2LEVEL_SUFFIXES = (
    ".co.kr", ".or.kr", ".go.kr", ".ac.kr", ".ne.kr", ".re.kr",
    ".co.jp", ".or.jp", ".ne.jp",
    ".co.uk", ".org.uk", ".gov.uk",
    ".com.au", ".com.cn", ".com.tw", ".com.hk", ".com.sg",
)


def extract_site_id(url: str) -> str:
    """
    URL 에서 site_id 자동 추출.
      siemreap.korean.net   → siemreap     (맨 앞 토큰이 skip 목록 밖 — 그대로)
      www.hanin.or.kr       → hanin        (www 스킵, .or.kr 절단)
      job.incruit.com       → incruit      (job 스킵)
      api.camhr.com         → camhr        (api 스킵)
      www.jobkorea.co.kr    → jobkorea     (www 스킵, .co.kr 절단)
      job.career.co.kr      → career       (job 스킵, .co.kr 절단)
      careers.lg.com        → lg           (careers 스킵)
    """
    host = urlparse(url).netloc.split(":")[0].lower()  # port 제거
    # 끝에 ccTLD 가 붙어있으면 통째로 잘라내야 "co" 같은 가짜 ID 안 나옴
    for suf in _CCTLD_2LEVEL_SUFFIXES:
        if host.endswith(suf):
            host = host[: -len(suf)]
            break
    else:
        # 일반 1-level TLD (.com, .net, .org, .kr 등) 마지막 토큰 잘라내기
        if "." in host:
            host = host.rsplit(".", 1)[0]
    parts = host.split(".")
    # 의미 없는 접두어 스킵 (단, 마지막 토큰 1개는 무조건 보존 → "site"로 빠지는 사고 방지)
    while len(parts) > 1 and parts[0] in _SUBDOMAIN_SKIP_PREFIXES:
        parts = parts[1:]
    if not parts:
        return "site"
    return parts[0] or "site"


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

    URL 구조(list_url / list_params)는 입력 url 을 권위 있는 소스로 사용한다.
    heuristic 이 주는 bbs_url/board_table 은 템플릿 조립 재료로 쓰지 않는다 —
    radiokorea(/community/jobs.php) 나 ppomppu(/zboard/zboard.php?id=guin) 처럼
    /bbs/board.php?bo_table= 표준에서 벗어난 변종을 gnuboard 로 오분류하는 경우에도
    사용자가 `add <URL>` 로 명시한 URL 이 그대로 보존돼야 하기 때문이다.

    heuristic 결과(c.config)는 HTML 구조 정보(selectors, parse_mode, theme)와
    base_url 폴백에만 사용.
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
        "skip_row_if_has_class": ["fz_list_th", "na-table-head"],
        # heuristic 이 상세링크에서 자동감지한 값 우선, 못 찾았으면 gnuboard 표준 'wr_id'
        "external_id_from_url_param": c.get("external_id_from_url_param") or "wr_id",
        "theme": c.get("theme", ""),  # 진단용 (크롤러는 안 씀)
    }


def _embedded_json_analysis_to_source(result, url: str) -> dict:
    """embedded_json 분석 결과 → source 블록.

    analyzer 가 내는 flat config (EmbeddedJSONStrategy):
        HTML 경로: {platform, base_url, script_selector, item_path, ...}
        렌더 경로: {platform, base_url, requires_render=True, state_source, item_path, ...}
    """
    parsed = urlparse(url)
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    list_params = {k: v for k, v in query_pairs if k.lower() != "page"}
    list_url = urlunparse(parsed._replace(query=""))

    c = result.config
    source = {
        "list_url": list_url,
        "list_params": list_params,
        "base_url": c.get("base_url") or f"{parsed.scheme}://{parsed.netloc}",
        "item_path": c.get("item_path", ""),
    }
    if c.get("requires_render"):
        source["state_source"] = c.get("state_source", "window.__NUXT__")
    else:
        source["script_selector"] = c.get("script_selector", "script#__NEXT_DATA__")
    return source


def _api_analysis_to_source(result, url: str) -> dict:
    """api_discovered 분석 결과 → source 블록.

    PlaywrightDiscoveryStrategy 가 내는 config:
        {platform, base_url, api_endpoint, method, request_headers,
         response_shape, response_sample, selection_source, llm_reason,
         all_candidates, needs_manual_adapter}

    이 중 크롤러가 쓸 것만 추려 source 로 재구성한다.
    api_endpoint 의 쿼리스트링은 list_params 로 분리하고,
    페이지 파라미터 후보가 섞여있으면 제거 (pagination.param 쪽으로 이동).
    """
    c = result.config
    endpoint = c.get("api_endpoint", "")
    parsed_ep = urlparse(endpoint)

    # 쿼리스트링 → list_params
    raw_params = dict(parse_qsl(parsed_ep.query, keep_blank_values=True))

    # 페이지 파라미터 추출 + list_params 에서 제거
    page_param = None
    for cand in API_PAGE_PARAM_CANDIDATES:
        if cand in raw_params:
            page_param = cand
            raw_params.pop(cand)
            break

    # api_endpoint 는 쿼리 제거된 순수 URL 로 저장
    endpoint_clean = urlunparse(parsed_ep._replace(query=""))

    # item_path 추론 — response_shape 기반
    shape = c.get("response_shape") or {}
    if shape.get("nested_array_path"):
        item_path = shape["nested_array_path"]
    elif shape.get("array_field"):
        item_path = shape["array_field"]
    elif shape.get("type") == "array":
        item_path = ""  # 루트 자체가 배열
    else:
        item_path = ""

    source = {
        "api_endpoint": endpoint_clean,
        "method": c.get("method", "GET"),
        "base_url": c.get("base_url", ""),
        "request_headers": c.get("request_headers") or {},
        "list_params": raw_params,
        "item_path": item_path,
    }
    # POST body — Playwright 가 캡처한 post_data 를 request_body 로 저장.
    # LG·토스 같이 POST 전용 + body 필수 API 대응. GET/None 이면 생략.
    post_data = c.get("post_data")
    if post_data and c.get("method", "").upper() == "POST":
        try:
            # 문자열로 잡힌 JSON 을 dict 로 파싱해두면 validator/api_crawler 가 바로 json= 로 전달.
            parsed_body = json.loads(post_data) if isinstance(post_data, str) else post_data
            source["request_body"] = parsed_body
        except Exception:
            # JSON 아니면 원본 문자열 그대로 (form-encoded 등)
            source["request_body"] = post_data
    return source, page_param


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

    if method not in EXTRACTION_METHOD_TO_CRAWLER:
        raise ValueError(
            f"extraction_method={method!r} 은 이 Phase 에서 구현 안 됨 — 등록 불가 "
            f"(site_type={site_type})"
        )

    api_page_param = None  # api 분기에서만 쓰임 — pagination 조립 전 보관

    if method == "dom":
        if site_type == "gnuboard":
            source = _gnuboard_analysis_to_source(result, url)
        elif site_type == "static_html":
            source = _static_html_analysis_to_source(result, url)
        else:
            # wordpress 등 DOM 이지만 변환 템플릿 아직 없음
            raise ValueError(
                f"site_type={site_type!r} DOM 변환 템플릿 미구현"
            )
    elif method == "embedded_json":
        source = _embedded_json_analysis_to_source(result, url)
    elif method == "api":
        source, api_page_param = _api_analysis_to_source(result, url)
    else:
        # 매핑에 있는데 변환 분기 안 탄 경우 — 방어
        raise ValueError(
            f"method={method!r} 변환 분기 없음 (분석만 되고 config 변환 미구현)"
        )

    requires_render = bool(result.config.get("requires_render"))

    if method == "api":
        pagination = {
            "type": "api_param",
            "param": api_page_param or "page",
            "start": 1,
        }
    else:
        pagination = {"type": "url_param", "param": "page", "start": 1}

    return {
        "extraction_method": method,
        "requires_render": requires_render,
        "source": source,
        "pagination": pagination,
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

    # 2. SPA 감지됐는데 Playwright 도 실패 (needs_playwright_discovery 만 있고
    #    실제 api_endpoint 는 비어있는 케이스)
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

    # 6. method 별 최소 구조 조건
    if method == "dom":
        selectors = config.get("selectors") or {}
        if not selectors.get("list_rows") or not selectors.get("subject_link"):
            return False, "selectors.list_rows / subject_link 비어있음 (필수)"
    elif method == "embedded_json":
        if config.get("requires_render"):
            if not config.get("state_source"):
                return False, (
                    "requires_render=True 지만 state_source 비어있음 "
                    "(렌더 결과 window.* 어떤 전역도 유효하지 않음)"
                )
        else:
            if not config.get("script_selector"):
                return False, "script_selector 비어있음 (필수)"
        if not config.get("item_path"):
            return False, "item_path 비어있음 — embedded state 안 배열 경로를 찾지 못했음"
    elif method == "api":
        if not config.get("api_endpoint"):
            return False, "api_endpoint 비어있음 (Playwright 가 후보 API 를 못 찾음)"
        # item_path 는 response_shape 에서 추론 — shape 에 배열이 있어야 변환 가능
        shape = config.get("response_shape") or {}
        has_array = (
            shape.get("nested_array_path")
            or shape.get("array_field")
            or shape.get("type") == "array"
        )
        if not has_array:
            return False, (
                "response_shape 에 배열 경로 없음 — 선택된 API 응답이 "
                "단일 객체/스칼라. 공고 리스트 API 아닐 가능성."
            )

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
