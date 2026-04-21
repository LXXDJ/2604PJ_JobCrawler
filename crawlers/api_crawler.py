"""
범용 API 크롤러

config.extraction_method == "api" 인 사이트를 처리한다.
PlaywrightDiscoveryStrategy 가 발견한 내부 JSON API 를 직접 호출하여 공고를 수집한다.
기존 camhr_crawler.py 의 하드코딩 로직을 일반화한 결과.

지원 경로:
    - GET / POST (JSON body) 두 method
    - pagination.type = "api_param": page 파라미터로 페이지 순회
    - item_path (dot-notation): 응답 JSON 안 공고 배열 경로
    - field_heuristic: embedded_crawler 와 동일 — title/company/url 등 키 후보 목록
    - detail endpoint (선택): 신규 공고에 한해 상세 API 호출

향후:
    - infinite scroll / cursor 기반 페이지네이션
    - JSONPath 표현식 (현재는 단순 dot-notation 만)
"""

import time
from typing import Any, Optional
from urllib.parse import urljoin

import requests

from database import JobDatabase


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# playwright 캡처 헤더 중 재전송하면 오히려 방해되는 것들.
# :authority 같은 HTTP/2 의사헤더와 세션성 헤더 제외.
HEADER_BLACKLIST = {":authority", ":method", ":path", ":scheme",
                    "cookie", "host", "content-length"}


# embedded_crawler 와 동일한 필드 휴리스틱. 중복이지만 모듈 의존 주기 싫어서 복제.
TITLE_KEYS = [
    "title", "jobTitle", "postSubject", "subject", "name",
    "jobName", "jobPostingName", "position", "positionName",
]
COMPANY_KEYS = [
    "company", "companyName", "compNm", "giupNm", "corpName",
    "employer", "employerName", "orgName",
]
URL_KEYS = [
    "url", "link", "detailUrl", "jobUrl", "detail_url",
    "permalink", "path", "href",
]
DATE_KEYS = [
    "pubdate", "pubDate", "publishedAt", "postedAt", "date", "regDate",
    "registeredAt", "createdAt", "openDate",
]
ID_KEYS = [
    "id", "jobId", "postId", "seq", "seqNo", "articleId",
    # 한국식 축약 — 벼룩시장 adId, 고용정보원 rcrtId, 사람인 계열 postSeq 등
    "adId", "rcrtId", "recId", "recruitId", "jobSeq", "postSeq",
    "giupSeq", "giupId", "pblntId",
]
LOCATION_KEYS = [
    "location", "locationName", "region", "regionName", "area", "cities",
    # 한국식: regnNm (지역명), arenm, rgNm
    "regnNm", "areNm", "rgNm", "workRegion",
]
SALARY_KEYS = [
    "salary", "salaryText", "wage", "pay", "salaryId",
    # 한국식: salAmt, salKind, payMonth
    "salAmt", "salKind", "payMonth",
]
JOB_TYPE_KEYS = [
    "jobType", "employmentType", "termId", "term",
    "wrkType",  # 벼룩시장
]

# 신규 공고 상세 조회 시 본문 후보 필드 (detail 응답에서 긁어옴)
CONTENT_KEYS = [
    "description", "content", "requirement", "body", "detail",
]


# ============================================================
# 공통 HTTP
# ============================================================

def _normalize_headers(raw: dict) -> dict:
    """캡처 헤더 정리 — 블랙리스트 제거 + User-Agent 강제."""
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    for k, v in (raw or {}).items():
        if k.startswith(":") or k.lower() in HEADER_BLACKLIST:
            continue
        headers[k] = v
    return headers


def _api_call(
    endpoint: str,
    method: str,
    params: dict,
    headers: dict,
    timeout: int,
    max_retries: int,
    retry_backoff: float,
) -> Any:
    """
    API 호출 + 재시도. 실패 시 마지막 예외 raise.
    GET 은 params 를 쿼리스트링으로, POST 는 JSON body 로 전달.
    """
    method_upper = method.upper()
    last_error = None

    for attempt in range(1, max_retries + 1):
        try:
            if method_upper == "GET":
                response = requests.get(
                    endpoint, params=params, headers=headers, timeout=timeout,
                )
            elif method_upper == "POST":
                response = requests.post(
                    endpoint, json=params, headers=headers, timeout=timeout,
                )
            else:
                raise ValueError(f"지원하지 않는 HTTP method: {method!r}")

            response.raise_for_status()
            if attempt > 1:
                print(f"      [retry] {attempt}회 시도 성공")
            return response.json()

        except requests.exceptions.HTTPError:
            # 4xx/5xx 는 재시도해도 동일 → 즉시 raise
            raise
        except Exception as e:
            last_error = e
            print(f"      [retry] {type(e).__name__}: {e} (attempt {attempt}/{max_retries})")

        if attempt < max_retries:
            time.sleep(retry_backoff * attempt)

    raise last_error


# ============================================================
# 경로 탐색 / 필드 추출
# ============================================================

def _traverse_path(state: Any, path: str) -> Any:
    """dot-notation path 로 state 내부 값 도달. 실패 시 None.

    [*] 와일드카드: `outer[*].inner` → outer 배열 각 아이템에서 inner 를 꺼내 flatten.
    벼룩시장(findall) 처럼 2단계 중첩 공고 리스트 대응 — validator 의 동명 함수와 동일 계약.
    """
    if not path:
        return state
    if "[*]" in path:
        before, _, after = path.partition("[*].")
        if not after:
            return None
        outer = _traverse_path(state, before) if before else state
        if not isinstance(outer, list):
            return None
        merged: list = []
        for item in outer:
            inner = _traverse_path(item, after)
            if inner is None:
                continue
            if isinstance(inner, list):
                merged.extend(inner)
            else:
                merged.append(inner)
        return merged
    cur = state
    for part in path.split("."):
        if isinstance(cur, dict):
            if part not in cur:
                return None
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit():
            idx = int(part)
            if idx >= len(cur):
                return None
            cur = cur[idx]
        else:
            return None
    return cur


def _pick(item: dict, keys: list) -> str:
    """item 에서 keys 중 첫 유효값 꺼내기. dict 값이면 name/text/value 내려 시도."""
    for k in keys:
        if k not in item:
            continue
        v = item[k]
        if v is None or v == "":
            continue
        if isinstance(v, (str, int, float)):
            s = str(v).strip()
            if s:
                return s
        if isinstance(v, dict):
            # dict 값이면 흔한 내부 키 순서대로 시도. "company" 는 camhr 처럼
            # employer: {company: "..."} 중첩 스키마 대응.
            for inner in ("label", "name", "text", "value", "title", "company"):
                if inner in v and v[inner]:
                    return str(v[inner]).strip()
    return ""


def _build_link(item: dict, base_url: str, link_template: str) -> str:
    """
    상세 링크 조립.
      1) item 에 URL 후보 키가 있으면 그걸 사용 (상대경로는 base_url 로 절대화)
      2) link_template 이 있으면 {id}/{key} 치환
      3) 없으면 빈 문자열
    """
    direct = _pick(item, URL_KEYS)
    if direct:
        if direct.startswith(("http://", "https://")):
            return direct
        if base_url:
            return urljoin(base_url.rstrip("/") + "/", direct.lstrip("/"))
        return direct

    if link_template:
        # {id}, {jobId} 등 key 치환 — item 의 값으로
        try:
            # format_map 으로 누락 key 시 KeyError → except 에서 빈 문자열
            return link_template.format_map(_LinkDict(item))
        except Exception:
            return ""
    return ""


class _LinkDict(dict):
    """link_template 의 {key} 에 item 의 값을 꽂을 때, 누락 key 는 빈 문자열로."""
    def __init__(self, item: dict):
        super().__init__()
        self._item = item

    def __missing__(self, key):
        v = self._item.get(key, "")
        return str(v) if v is not None else ""


def _build_content(detail: Any, content_path: str) -> str:
    """상세 응답에서 본문 추출.
    content_path 가 있으면 그 경로 사용, 없으면 CONTENT_KEYS 후보 중 첫 번째.
    """
    if detail is None:
        return ""
    if content_path:
        val = _traverse_path(detail, content_path)
        if isinstance(val, str):
            return val.strip()
        return ""
    if isinstance(detail, dict):
        parts = []
        for key in CONTENT_KEYS:
            v = detail.get(key)
            if isinstance(v, str) and v.strip():
                parts.append(f"[{key}]\n{v.strip()}")
        return "\n\n".join(parts)
    return ""


# ============================================================
# 아이템 → job dict 매핑
# ============================================================

def _build_job(
    item: dict,
    site_id: str,
    base_url: str,
    link_template: str,
    detail: Optional[dict] = None,
) -> Optional[dict]:
    """단일 아이템을 JobDatabase 가 기대하는 모양으로 변환. 실패 시 None."""
    title = _pick(item, TITLE_KEYS)
    if not title:
        return None

    link = _build_link(item, base_url, link_template)
    ext_id = _pick(item, ID_KEYS) or link or title

    raw_data = {"list": item}
    content = ""
    if detail is not None:
        raw_data["detail"] = detail
        content = _build_content(detail, "")

    return {
        "source": site_id,
        "external_id": str(ext_id),
        "title": title,
        "company": _pick(item, COMPANY_KEYS),
        "location": _pick(item, LOCATION_KEYS),
        "salary": _pick(item, SALARY_KEYS),
        "job_type": _pick(item, JOB_TYPE_KEYS),
        "pub_date": _pick(item, DATE_KEYS),
        "link": link,
        "content": content,
        "raw_data": raw_data,
    }


# ============================================================
# 메인 크롤링 진입점
# ============================================================

def crawl(
    site_id: str,
    config: dict,
    db_path: str,
    max_pages: Optional[int] = None,
    http_config: Optional[dict] = None,
):
    """
    API 기반 사이트 크롤링 + DB 저장.

    config 기대 모양:
        {
            "extraction_method": "api",
            "source": {
                "api_endpoint": "https://api.example.com/v1/jobs/page-query",
                "method": "GET",
                "base_url": "https://www.example.com",
                "request_headers": {"Referer": "..."},
                "list_params": {"size": 50},
                "item_path": "data.result",
                "total_path": "data.totalPage",           # 선택 — 총 페이지 수 경로
                "link_template": "https://www.example.com/a/job/{id}",  # 선택
                "detail_endpoint_template": "https://api.example.com/v1/jobs/{id}",  # 선택
                "detail_method": "GET",                   # 선택 (기본 GET)
                "detail_content_path": "data.description" # 선택 — 본문 경로
            },
            "pagination": {
                "type": "api_param",
                "param": "page",          # 페이지 번호 파라미터 이름
                "start": 1,
                "max_pages": 3            # 선택
            }
        }
    """
    source = config.get("source") or {}
    pagination = config.get("pagination") or {}

    endpoint = source.get("api_endpoint")
    if not endpoint:
        raise ValueError(f"[{site_id}] source.api_endpoint 비어있음")

    method = source.get("method", "GET")
    base_url = source.get("base_url", "")
    item_path = source.get("item_path", "")
    total_path = source.get("total_path", "")
    link_template = source.get("link_template", "")
    base_list_params = dict(source.get("list_params") or {})
    headers = _normalize_headers(source.get("request_headers") or {})

    detail_endpoint_template = source.get("detail_endpoint_template", "")
    detail_method = source.get("detail_method", "GET")
    detail_content_path = source.get("detail_content_path", "")

    http_kwargs = {
        "timeout": (http_config or {}).get("timeout", 30),
        "max_retries": (http_config or {}).get("max_retries", 3),
        "retry_backoff": (http_config or {}).get("retry_backoff", 2.0),
    }

    print("=" * 60)
    print(f"[{site_id}] API 크롤러 시작")
    print(f"    endpoint   : {method} {endpoint}")
    print(f"    item_path  : {item_path}")
    if detail_endpoint_template:
        print(f"    detail     : {detail_method} {detail_endpoint_template}")
    print("=" * 60)

    db = JobDatabase(db_path)
    db.init_schema()
    run_id = db.start_crawl_run(site_id)

    new_count = 0
    updated_count = 0
    page_param = pagination.get("param", "page")
    start_page = pagination.get("start", 1)

    try:
        # 1) 첫 페이지로 전체 규모 파악
        first_params = dict(base_list_params)
        first_params[page_param] = start_page
        first_response = _api_call(
            endpoint, method, first_params, headers, **http_kwargs,
        )

        total_pages_from_api = None
        if total_path:
            t = _traverse_path(first_response, total_path)
            if isinstance(t, (int, float)):
                total_pages_from_api = int(t)

        cap = max_pages or pagination.get("max_pages")
        if total_pages_from_api is None and not cap:
            # 총 페이지 모르고 cap 도 없으면 일단 1페이지만 (안전장치).
            # 운영에서는 cap 을 명시하는 게 안전.
            total_pages = 1
            print("    [WARN] total_path 없고 max_pages 미지정 → 1페이지만 수집")
        else:
            total_pages = total_pages_from_api or cap
            if cap and total_pages_from_api:
                total_pages = min(total_pages_from_api, cap)

        # early termination: dom_crawler 와 동일 정책.
        stop_threshold = pagination.get("consecutive_existing_stop", 30)
        consecutive_existing = 0

        print(f"\n    수집할 페이지: {start_page} ~ {start_page + total_pages - 1}"
              + (f"  (연속 기존 {stop_threshold}건 시 조기종료)"
                 if stop_threshold else ""))

        # 2) 페이지 순회
        for page in range(start_page, start_page + total_pages):
            if page == start_page:
                response_json = first_response
            else:
                params = dict(base_list_params)
                params[page_param] = page
                try:
                    response_json = _api_call(
                        endpoint, method, params, headers, **http_kwargs,
                    )
                except Exception as e:
                    print(f"    [WARN] page={page} API 호출 실패 — 중단: "
                          f"{type(e).__name__}: {e}")
                    break
                time.sleep(0.3)

            items = _traverse_path(response_json, item_path)
            if not isinstance(items, list):
                print(f"    [WARN] page={page} item_path={item_path!r} 가 배열 아님 — 중단")
                break

            print(f"\n[{page}/{start_page + total_pages - 1}] {len(items)}건 처리 중...")
            if not items:
                print("    (빈 페이지 — 중단)")
                break

            for item in items:
                if not isinstance(item, dict):
                    continue

                # external_id 만 임시 계산 — 존재 확인용
                probe_ext_id = _pick(item, ID_KEYS) or _build_link(item, base_url, link_template) or ""
                if not probe_ext_id:
                    continue

                existing = _check_exists(db, site_id, str(probe_ext_id))

                detail = None
                if not existing and detail_endpoint_template:
                    # 신규만 상세 API 호출 — 재확인 시 부하 최소화
                    try:
                        detail_url = detail_endpoint_template.format_map(_LinkDict(item))
                        detail_response = _api_call(
                            detail_url, detail_method, {}, headers, **http_kwargs,
                        )
                        # 상세 응답의 실데이터가 한 단계 안쪽에 있는 경우 (data 필드) 자동 내림.
                        if isinstance(detail_response, dict) and "data" in detail_response:
                            detail = detail_response["data"]
                        else:
                            detail = detail_response
                        time.sleep(0.3)
                    except Exception as e:
                        print(f"      [WARN] 상세 API 실패 ({probe_ext_id}): "
                              f"{type(e).__name__}: {e}")

                job = _build_job(item, site_id, base_url, link_template, detail)
                if job is None:
                    continue

                if existing:
                    db.upsert_job(job)
                    updated_count += 1
                    consecutive_existing += 1
                else:
                    # detail_content_path 가 있으면 상세에서 그 경로로 다시 뽑기 (덮어씀)
                    if detail and detail_content_path:
                        val = _traverse_path(detail, detail_content_path)
                        if isinstance(val, str):
                            job["content"] = val.strip()
                    db.upsert_job(job)
                    new_count += 1
                    consecutive_existing = 0
                    print(f"      [NEW] {job['title'][:60]}")

            print(f"    누적: 신규 {new_count}, 기존 {updated_count}"
                  f"  (연속 기존 {consecutive_existing})")

            if stop_threshold and consecutive_existing >= stop_threshold:
                print(f"    [조기종료] 연속 기존 {consecutive_existing} >= {stop_threshold}"
                      f" — page {page} 에서 중단")
                break

        db.finish_crawl_run(run_id, new_count, updated_count)

    except Exception as e:
        db.finish_crawl_run(run_id, new_count, updated_count, error=str(e))
        raise

    print(f"\n{'=' * 60}")
    print(f"[{site_id}] 크롤링 완료 — 신규 {new_count}건 / 재확인 {updated_count}건")
    print(f"{'=' * 60}")

    return {"new": new_count, "updated": updated_count}


def _check_exists(db, source: str, external_id: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        ).fetchone()
        return row is not None
