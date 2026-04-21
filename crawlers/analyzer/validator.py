"""
config 검증기 — LLM 환각 방어선

analyzer 가 생성한 config 를 sites.json 에 저장하기 전에, 실제로 돌려봤을 때
의미 있는 데이터가 나오는지 확인한다.

이게 통과하지 못한 config 는 `validated=true` 가 붙지 않고, sites_registry 가
저장을 거부한다. 덕분에 LLM 이 엉뚱한 selectors 를 제안해도 "프로덕션에서
0건 수집" 같은 침묵 실패를 방지할 수 있다.

Phase 2 범위:
    - validate_dom_config          : 실제 구현 (Phase 1 부터)
    - validate_embedded_json_config: 실제 구현 (Phase 2 추가)
    - validate_api_config          : stub (Phase 3)
"""

import json
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup


# DOM validator 에 해당 임계값들
MIN_LIST_ROWS = 2          # list_rows 셀렉터가 최소 이 만큼 매칭돼야 통과
MIN_TITLE_LEN = 3          # 너무 짧은 제목 (아이콘/숫자 한 글자 등) 은 신뢰 불가
MIN_TITLE_MATCH_RATIO = 0.5  # row 중 subject_link 에서 제목 뽑힌 비율이 이거 미만이면 실패

# 제목이 아닌 UI/네비 단어 목록 — 혼자 있을 때만 걸러냄 (부분 일치는 허용)
UI_NOISE_TITLES = {
    "로그인", "로그아웃", "회원가입", "마이페이지", "홈", "검색",
    "이전", "다음", "처음", "마지막", "더보기",
    "공지", "공지사항",
    "login", "logout", "search", "home", "more",
}

# API validator 전용 — "필터옵션/코드테이블 vs 진짜 공고 리스트" 판별용 2차 시그널.
# LLM Ranker 가 가끔 GraphQL 필터옵션·브랜드코드 API 를 공고 리스트로 오인식하는 사고를 막는다.
# 필터옵션은 보통 {id, name} 짧은 필드 몇개 / 코드 2~6자. 진짜 공고는 제목 10자+ 회사/날짜/URL 보유.

MIN_AVG_TITLE_LEN_API = 8   # 필터코드(2~6자)는 걸러내고 짧은 공고제목은 남기는 경계선
MIN_SECOND_SIGNAL_RATIO = 0.5  # 아이템 중 회사/날짜/URL/위치 중 하나라도 가진 비율 하한

# 아이템에서 "2차 시그널" 로 인정할 필드명 (snake/camel/한국어축약 혼재 허용).
# _normalize_key 가 소문자화 + 언더스코어/하이픈 제거하므로 여기 값은 전부 소문자 연속체.
API_SECOND_SIGNAL_KEYS = {
    # 회사/작성자
    "company", "companyname", "compnm", "corp", "corpname",
    "author", "employer", "giupnm", "giupname", "orannm", "writer", "writernm",
    "mbizname", "bizname",
    # 날짜 (등록/마감/갱신 어느거든)
    "date", "posteddate", "postedat", "createdat", "updatedat", "regdate",
    "regdt", "registdt", "modifiedat", "enddate", "closeddate", "deadline",
    "startdate", "opendate",
    # LG / 공공기관 축약
    "recenddatetime", "recstartdatetime", "recdatediff", "createymd",
    # 위치 (regnNm, arenm, rgNm 같은 한국식 축약 포함)
    "location", "region", "area", "city", "address", "workplace", "worklocation",
    "workregion", "loc", "regnnm", "rgnm", "arenm", "areanm",
    # URL/링크
    "url", "link", "href", "detailurl", "joburl", "applyurl",
    "permalink", "detailhref",
    # 급여/근무형태 (salAmt, salKind, payMin, wageType 등 축약형)
    "salary", "pay", "salarymin", "salarymax", "wage",
    "salamt", "salkind", "salpossible", "salpaytype", "paymonth", "paymin", "paymax",
    "employmenttype", "jobtype", "worktype", "contracttype", "wrktype", "wrktmcd",
    # ID/공고코드 (adId, rcrtId, postSeq 같이 URL 생성용)
    "adid", "jobid", "postid", "recruitid", "rcrtid", "postseq", "jobseq",
    # 카테고리 (약한 시그널이지만 있는 경우 많음)
    "category", "jobcategory", "occupation", "industry", "jobcd", "jobcdlist", "jobkolist",
}

# 코드테이블 패턴 — 아이템이 거의 이 필드들로만 구성되면 "메타데이터" 로 간주.
CODE_TABLE_KEYS = {"id", "code", "name", "value", "label", "key", "text", "title"}
CODE_TABLE_MAX_KEYS = 3    # 3개 이하 필드만 가진 아이템이 다수면 코드테이블로 봄
CODE_TABLE_RATIO = 0.8     # 80% 이상 아이템이 이 패턴이면 거부


@dataclass
class ValidationReport:
    """config 검증 결과. sites.json 엔트리에 그대로 박힌다."""
    ok: bool
    reason: str = ""                              # 실패 시 사람 읽을 이유
    items_extracted: int = 0
    fields_matched: dict = field(default_factory=dict)   # {"title": N, "author": M, ...}
    sample_titles: list = field(default_factory=list)    # 첫 3개 제목 (디버그용)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "reason": self.reason,
            "items_extracted": self.items_extracted,
            "fields_matched": self.fields_matched,
            "sample_titles": self.sample_titles,
        }


# ============================================================
# DOM config 검증
# ============================================================

def validate_dom_config(html: str, config: dict) -> ValidationReport:
    """
    DOM 기반 config 가 실제 HTML 에서 의미 있는 아이템을 추출할 수 있는지 검증.

    기대하는 config 모양:
        {
            "source": {
                "selectors": {
                    "list_rows": "...",
                    "subject_link": "...",
                    "author": "...",       # 선택
                    "date": "...",         # 선택
                    "hit": "...",          # 선택
                },
                "skip_row_if_has_class": ["fz_list_th", ...],  # 선택
            }
        }

    통과 조건:
        1. list_rows 로 매칭된 행이 ≥ MIN_LIST_ROWS
        2. 헤더 행 필터링 후에도 ≥ MIN_LIST_ROWS 남음
        3. 남은 행 중 subject_link 로 제목 뽑히는 비율 ≥ MIN_TITLE_MATCH_RATIO
        4. 뽑힌 제목이 UI 노이즈만은 아님
    """
    source = config.get("source") or {}
    selectors = source.get("selectors") or {}

    list_rows_sel = selectors.get("list_rows")
    subject_link_sel = selectors.get("subject_link")

    if not list_rows_sel:
        return ValidationReport(ok=False, reason="selectors.list_rows 가 비어있음")
    if not subject_link_sel:
        return ValidationReport(ok=False, reason="selectors.subject_link 이 비어있음")

    try:
        soup = BeautifulSoup(html, "lxml")
    except Exception as e:
        return ValidationReport(ok=False, reason=f"HTML 파싱 실패: {type(e).__name__}: {e}")

    rows = soup.select(list_rows_sel)
    if len(rows) < MIN_LIST_ROWS:
        return ValidationReport(
            ok=False,
            reason=f"list_rows 매칭 {len(rows)}개 (< {MIN_LIST_ROWS})",
            items_extracted=len(rows),
        )

    # 헤더 행 필터 — dom_crawler 와 동일 로직 사용
    skip_classes = set(source.get("skip_row_if_has_class") or [])
    data_rows = []
    for row in rows:
        row_classes = set(row.get("class") or [])
        if skip_classes & row_classes:
            continue
        # 내부 자손에 skip 클래스 있는 경우도 건너뛰기 (테이블 헤더가 tr 안 td 클래스로 표시되는 경우)
        if skip_classes and any(
            row.select_one(f".{cls}") for cls in skip_classes
        ):
            continue
        data_rows.append(row)

    if len(data_rows) < MIN_LIST_ROWS:
        return ValidationReport(
            ok=False,
            reason=(
                f"헤더 필터 후 데이터 행 {len(data_rows)}개 (< {MIN_LIST_ROWS}) "
                f"— list_rows 가 너무 광범위하거나 헤더 필터가 과함"
            ),
            items_extracted=len(rows),
        )

    # 제목 추출 시도
    titles = []
    fields_matched = {"title": 0}
    for row in data_rows:
        link_tag = row.select_one(subject_link_sel)
        if not link_tag:
            continue
        title = link_tag.get_text(strip=True)
        if len(title) >= MIN_TITLE_LEN:
            titles.append(title)
            fields_matched["title"] += 1

    match_ratio = fields_matched["title"] / len(data_rows)
    if match_ratio < MIN_TITLE_MATCH_RATIO:
        return ValidationReport(
            ok=False,
            reason=(
                f"subject_link 으로 제목 뽑힌 비율 {match_ratio:.0%} "
                f"({fields_matched['title']}/{len(data_rows)}) < {MIN_TITLE_MATCH_RATIO:.0%}"
            ),
            items_extracted=len(data_rows),
            fields_matched=fields_matched,
            sample_titles=titles[:3],
        )

    if _all_ui_noise(titles):
        return ValidationReport(
            ok=False,
            reason="추출된 제목이 모두 UI/네비 키워드 — selectors 가 메뉴/페이지네이션을 가리킬 가능성",
            items_extracted=len(data_rows),
            fields_matched=fields_matched,
            sample_titles=titles[:3],
        )

    # 선택적 필드 (author/date/hit) 도 몇 개나 매칭되는지 기록만 (실패 사유 X)
    for field_name in ("author", "date", "hit"):
        sel = selectors.get(field_name)
        if not sel:
            continue
        count = sum(1 for row in data_rows if row.select_one(sel))
        fields_matched[field_name] = count

    return ValidationReport(
        ok=True,
        reason="",
        items_extracted=len(data_rows),
        fields_matched=fields_matched,
        sample_titles=titles[:3],
    )


def _all_ui_noise(titles: list) -> bool:
    """제목 배열 전체가 UI 노이즈 키워드 뿐인지 확인."""
    if not titles:
        return True
    for t in titles:
        stripped = t.strip().lower()
        if stripped not in UI_NOISE_TITLES:
            return False
    return True


# ============================================================
# API config 검증
# ============================================================

VALIDATOR_API_TIMEOUT = 15
VALIDATOR_API_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def validate_api_config(config: dict) -> ValidationReport:
    """
    API 엔드포인트를 실제로 호출해서 응답이 공고 리스트 형태인지 확인.

    기대하는 config 모양:
        {
            "extraction_method": "api",
            "source": {
                "api_endpoint": "https://api.example.com/v1/jobs",
                "method": "GET",
                "request_headers": {"Referer": "...", ...},   # 선택
                "list_params": {"page": 1, "size": 50},       # 선택
                "item_path": "data.result",
            }
        }

    통과 조건:
        1. 실제 호출 → HTTP 200 + JSON 파싱 성공
        2. item_path 로 도달한 값이 list 이고 길이 >= MIN_LIST_ROWS
        3. 아이템 중 제목성 필드를 가진 비율 >= MIN_TITLE_MATCH_RATIO
    """
    source = config.get("source") or {}
    api_endpoint = source.get("api_endpoint")
    method = (source.get("method") or "GET").upper()
    item_path = source.get("item_path", "")
    list_params = source.get("list_params") or {}
    headers = {"User-Agent": VALIDATOR_API_USER_AGENT, "Accept": "application/json"}
    extra_headers = source.get("request_headers") or {}
    for k, v in extra_headers.items():
        # playwright 캡처 헤더에는 :authority 같은 HTTP/2 의사헤더 / cookie 가 섞임 — 제외
        if k.startswith(":") or k.lower() in ("cookie", "host", "content-length"):
            continue
        headers[k] = v

    if not api_endpoint:
        return ValidationReport(ok=False, reason="source.api_endpoint 비어있음")
    if not item_path:
        return ValidationReport(ok=False, reason="source.item_path 비어있음")

    # POST body — Playwright 가 캡처한 실제 요청 body. LG·토스 같이 검색조건이 body 에
    # 담겨 보내지는 API 대응. request_body 가 없으면 기존대로 list_params 를 body 로 씀.
    request_body = source.get("request_body")

    try:
        if method == "GET":
            response = requests.get(
                api_endpoint,
                params=list_params,
                headers=headers,
                timeout=VALIDATOR_API_TIMEOUT,
            )
        elif method == "POST":
            post_json = request_body if request_body is not None else list_params
            response = requests.post(
                api_endpoint,
                json=post_json,
                params=list_params if request_body is not None else None,
                headers=headers,
                timeout=VALIDATOR_API_TIMEOUT,
            )
        else:
            return ValidationReport(
                ok=False,
                reason=f"method={method!r} 미지원 (GET/POST 만 지원)",
            )
    except Exception as e:
        return ValidationReport(
            ok=False,
            reason=f"API 호출 실패: {type(e).__name__}: {e}",
        )

    if response.status_code != 200:
        return ValidationReport(
            ok=False,
            reason=f"HTTP {response.status_code} — endpoint 가 인증/세션/Referer 를 요구할 가능성",
        )

    try:
        payload = response.json()
    except Exception as e:
        return ValidationReport(
            ok=False,
            reason=f"응답 JSON 파싱 실패: {type(e).__name__}: {e}",
        )

    items = _traverse_path(payload, item_path)
    if items is None:
        return ValidationReport(
            ok=False,
            reason=f"item_path {item_path!r} 도달 실패 (응답 구조 변경?)",
        )
    if not isinstance(items, list):
        return ValidationReport(
            ok=False,
            reason=f"item_path 끝 값이 list 아님 (type={type(items).__name__})",
        )
    if len(items) < MIN_LIST_ROWS:
        return ValidationReport(
            ok=False,
            reason=f"배열 길이 {len(items)} < {MIN_LIST_ROWS}",
            items_extracted=len(items),
        )

    # --- 자동 path 재탐색 (Gatsby/Strapi/GraphQL 대응) ---
    # Playwright 가 item_path 를 '첫 list' 기준으로 잡으면 당근/라인 같은 Gatsby
    # page-data 에선 header_entries(4) · locales.edges(1) 등 노이즈 배열이 먼저 잡힘.
    # 실제 공고 배열은 같은 응답 안 다른 경로에 있음 — _find_best_title_array 로 응답
    # 전체를 walk 해서 title-rich 배열 경로 자동 발견 후 item_path 업그레이드.
    outer_sample_has_title = any(
        isinstance(it, dict) and _pick_title(it) for it in items[:5]
    )
    if not outer_sample_has_title:
        better_path, better_list = _find_best_title_array(payload)
        # 원 item_path 보다 명백히 큰 배열 + 제목 매치 존재해야 전환
        if better_path and len(better_list) > max(len(items), MIN_LIST_ROWS):
            source["item_path"] = better_path
            items = better_list
            print(
                f"      [validator] item_path 재탐색: {item_path!r} → {better_path!r} "
                f"({len(items)}건, 제목 풍부)"
            )

    # --- 2단계 중첩 자동 탐지 (위 재탐색도 실패한 경우 폴백) ---
    # 외부 아이템들이 제목 필드를 전혀 안 갖고 있지만, 각 아이템 안에 "제목 있는 dict
    # 리스트" 필드가 있다면 그게 실제 공고 리스트. 벼룩시장(findall) 의
    # data.partTimeJobList[*].jobAdList 같은 케이스.
    outer_sample_has_title = any(
        isinstance(it, dict) and _pick_title(it) for it in items[:5]
    )
    if not outer_sample_has_title:
        nested_field = _detect_nested_list_field(items)
        if nested_field:
            flattened: list = []
            for it in items:
                if isinstance(it, dict):
                    sub = it.get(nested_field)
                    if isinstance(sub, list):
                        flattened.extend(sub)
            if len(flattened) >= MIN_LIST_ROWS:
                # item_path 를 2단계 형태로 업그레이드 — 저장/크롤러 모두 [*] 지원
                new_item_path = (
                    f"{item_path}[*].{nested_field}" if item_path else f"[*].{nested_field}"
                )
                # source 는 config["source"] 의 레퍼런스 — mutate 하면 sites.json 에도
                # 새 경로로 저장된다 (analyzer → validator → registry 까지 같은 dict 공유).
                source["item_path"] = new_item_path
                items = flattened
                print(
                    f"      [validator] item_path 2단계 업그레이드: "
                    f"{item_path!r} → {new_item_path!r} ({len(flattened)}건)"
                )

    titles: list = []
    matched = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        picked = _pick_title(item)
        if picked and len(picked) >= MIN_TITLE_LEN:
            titles.append(picked)
            matched += 1

    fields_matched = {"title": matched}
    match_ratio = matched / len(items)
    if match_ratio < MIN_TITLE_MATCH_RATIO:
        return ValidationReport(
            ok=False,
            reason=(
                f"제목성 필드 추출 비율 {match_ratio:.0%} ({matched}/{len(items)}) "
                f"< {MIN_TITLE_MATCH_RATIO:.0%} — item_path 가 메타/필터 배열일 가능성"
            ),
            items_extracted=len(items),
            fields_matched=fields_matched,
            sample_titles=titles[:3],
        )

    if _all_ui_noise(titles):
        return ValidationReport(
            ok=False,
            reason="추출된 제목이 모두 UI/네비 키워드",
            items_extracted=len(items),
            fields_matched=fields_matched,
            sample_titles=titles[:3],
        )

    # --- 2차 시그널 검증 (필터옵션/코드테이블 가드) ---
    # (1) 제목 평균 길이 — 필터코드는 2~6자, 공고는 보통 15자+
    avg_title_len = sum(len(t) for t in titles) / len(titles) if titles else 0
    if avg_title_len < MIN_AVG_TITLE_LEN_API:
        return ValidationReport(
            ok=False,
            reason=(
                f"제목 평균 길이 {avg_title_len:.1f}자 < {MIN_AVG_TITLE_LEN_API}자 "
                f"— 필터옵션/코드테이블 API 가능성"
            ),
            items_extracted=len(items),
            fields_matched=fields_matched,
            sample_titles=titles[:3],
        )

    # (2) 코드테이블 패턴 — 아이템이 {id/code/name/value/label} 같은 짧은 필드셋만 가지면 거부
    if _is_code_table_pattern(items):
        return ValidationReport(
            ok=False,
            reason=(
                f"아이템이 {{id,code,name,...}} 류 필드 ≤{CODE_TABLE_MAX_KEYS}개로만 구성 "
                f"({CODE_TABLE_RATIO:.0%}+) — 코드테이블/필터옵션 API"
            ),
            items_extracted=len(items),
            fields_matched=fields_matched,
            sample_titles=titles[:3],
        )

    # (3) 2차 시그널 비율 — 회사/날짜/위치/URL/급여 중 하나라도 가진 아이템 비율
    second_signal_count = sum(
        1 for item in items
        if isinstance(item, dict) and _has_second_signal(item)
    )
    second_signal_ratio = second_signal_count / len(items)
    fields_matched["second_signal"] = second_signal_count
    if second_signal_ratio < MIN_SECOND_SIGNAL_RATIO:
        return ValidationReport(
            ok=False,
            reason=(
                f"2차 시그널(회사/날짜/위치/URL) 비율 {second_signal_ratio:.0%} "
                f"({second_signal_count}/{len(items)}) < {MIN_SECOND_SIGNAL_RATIO:.0%} "
                f"— 메타데이터 API 가능성"
            ),
            items_extracted=len(items),
            fields_matched=fields_matched,
            sample_titles=titles[:3],
        )

    return ValidationReport(
        ok=True,
        items_extracted=len(items),
        fields_matched=fields_matched,
        sample_titles=titles[:3],
    )


def _normalize_key(key: str) -> str:
    """snake/camel 혼재를 맞추기 위해 소문자화 + 언더스코어 제거."""
    return key.replace("_", "").replace("-", "").lower()


def _has_second_signal(item: dict) -> bool:
    """아이템이 2차 시그널 필드 (회사/날짜/URL 등) 를 하나라도 가지면 True.

    top-level 만 체크. nested 는 들여다보지 않는다 (거짓양성 위험).
    """
    for key in item.keys():
        if not isinstance(key, str):
            continue
        if _normalize_key(key) in API_SECOND_SIGNAL_KEYS:
            # 값이 공백/None 만이 아닌지도 확인 — 필드가 있어도 빈 문자열이면 의미없음
            v = item[key]
            if v is None:
                continue
            if isinstance(v, str) and not v.strip():
                continue
            return True
    return False


def _find_best_title_array(payload: Any) -> tuple[Optional[str], list]:
    """응답 전체를 walk 해서 'title 있는 dict 가 가장 많은 배열' 의 경로 + 배열 반환.

    Playwright 의 _infer_shape 가 '첫 list' 기준으로 item_path 를 잡을 때, 실제 공고
    배열이 더 깊은 경로에 있거나 뒤에 있으면 놓치는 문제 해결용. 당근
    (result.data.allDepartmentFilteredJobPost.nodes, 49건) / 라인
    (result.data.allStrapiJobs.edges, 364건) 이 정확히 이 케이스.

    반환: (dot-path, list). 탐색 실패시 (None, []).
    """
    best_path: Optional[str] = None
    best_list: list = []
    best_count = 0

    def walk(obj: Any, path: str):
        nonlocal best_path, best_list, best_count
        # 배열 + 첫 아이템이 dict 이면 이 배열의 title 매치 수 집계
        if isinstance(obj, list) and obj and all(isinstance(x, dict) for x in obj[:5]):
            # _pick_title 은 wrapper unwrap(node/data/attributes) 지원 —
            # 라인 edges[i].node.title 형태도 잡힌다.
            count = sum(1 for x in obj if isinstance(x, dict) and _pick_title(x))
            if count > best_count:
                best_path = path
                best_list = obj
                best_count = count
        # dict 재귀
        if isinstance(obj, dict):
            for k, v in obj.items():
                new_path = f"{path}.{k}" if path else k
                walk(v, new_path)
        # list 재귀는 생략 — item_path 는 list 를 가리키는 것이지 list 내부 index 재귀 아님

    walk(payload, "")

    # wrapper unwrap 시도 — 모든 아이템이 {node: {...}} 같은 구조면 unwrap 해서
    # 2차 시그널 필드가 직접 item top-level 에 보이도록. item_path 는 [*].wrapper 로.
    if best_list and best_path:
        for wrapper in TITLE_WRAPPER_KEYS:
            if all(
                isinstance(it, dict) and wrapper in it and isinstance(it[wrapper], dict)
                for it in best_list
            ):
                unwrapped = [it[wrapper] for it in best_list]
                # unwrap 후 title 매치 수가 감소하지 않아야 유의미
                new_count = sum(1 for x in unwrapped if _pick_title(x))
                if new_count >= best_count:
                    return f"{best_path}[*].{wrapper}", unwrapped
                break

    return best_path, best_list


def _detect_nested_list_field(outer_items: list) -> Optional[str]:
    """외부 아이템들이 공통적으로 가진 'dict 리스트' 필드명을 찾는다.

    반환 조건:
        - 최소 2개 외부 아이템에 같은 필드명의 list-of-dict 가 존재
        - 그 내부 아이템에 제목성 필드가 보임 (임의 샘플 기준)
        - 그런 필드가 여러 개면 제목성 매치가 가장 많은 것 선택

    벼룩시장의 `data.partTimeJobList[*].jobAdList` 같은 2단계 구조 자동 감지용.
    """
    if not outer_items:
        return None
    candidate_scores: dict = {}
    for it in outer_items[:10]:  # 앞 10개만 샘플
        if not isinstance(it, dict):
            continue
        for k, v in it.items():
            if not isinstance(v, list) or not v:
                continue
            if not isinstance(v[0], dict):
                continue
            # 해당 sub-list 의 아이템들에 제목이 얼마나 있는지 집계
            hit = sum(1 for x in v[:5] if isinstance(x, dict) and _pick_title(x))
            if hit > 0:
                candidate_scores[k] = candidate_scores.get(k, 0) + hit
    if not candidate_scores:
        return None
    # 제목 매치 총합이 가장 높은 필드 선택
    best = max(candidate_scores.items(), key=lambda kv: kv[1])
    return best[0] if best[1] >= 2 else None


def _is_code_table_pattern(items: list) -> bool:
    """아이템들이 {id,code,name} 류 짧은 필드셋으로만 구성되면 True.

    80%+ 아이템이 3개 이하 필드 + 그 필드들이 전부 코드테이블 키셋에 속하면 거부.
    """
    if not items:
        return False
    pattern_count = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        keys_norm = {_normalize_key(k) for k in item.keys() if isinstance(k, str)}
        if len(keys_norm) > CODE_TABLE_MAX_KEYS:
            continue
        if keys_norm <= CODE_TABLE_KEYS:  # 모든 키가 코드테이블 셋 안에
            pattern_count += 1
    return pattern_count / len(items) >= CODE_TABLE_RATIO


# ============================================================
# Embedded JSON config 검증
# ============================================================

# 공고 아이템 내에서 "제목성" 필드로 간주할 key 후보.
# embedded_crawler._pick_field 와 기본 의도는 동일하지만, 여기선 validator 만의
# 책임 (실제 배열에 제목이 있는가) 을 위해 독립적으로 정의해둔다.
EMBEDDED_TITLE_KEYS = [
    "title", "jobTitle", "postSubject", "subject", "name",
    "jobName", "jobPostingName", "position", "positionName",
    # 한국 사이트 축약 — 알바몬 recruitTitle, 공공기관 rcrtTitle/empmnTitle 등
    "recruitTitle", "postingTitle", "rcrtTitle", "rcrtSj", "empmnTitle",
    "pblntTitle", "boardTitle", "listSj", "bidNm",
    # 카카오 jobOfferTitle, 라인 title_en, 토스 post_title 등 추가 축약
    "jobOfferTitle", "title_en", "post_title",
    # LG careers jobNoticeName, 일부 공공기관 variants
    "jobNoticeName", "noticeTitle", "recruitName", "recruitNoticeName",
]

# GraphQL/Gatsby/Strapi 의 edges[*].node 또는 { data: {...} } 같은 wrapper 패턴.
# _pick_title 이 아이템 안에서 바로 title 못 찾으면 이 키들을 한 단계 unwrap 해서
# 재탐색. 라인 careers (allStrapiJobs.edges[i].node.title) 대응용.
TITLE_WRAPPER_KEYS = ("node", "data", "attributes", "fields", "item")


def validate_embedded_json_config(
    html: str,
    config: dict,
    url: Optional[str] = None,
) -> ValidationReport:
    """
    config 가 실제로 공고성 데이터를 추출할 수 있는지 확인.

    HTML 경로 (config.requires_render=False): 주어진 html 에서 script 태그 파싱
    렌더 경로 (config.requires_render=True):  url 로 Playwright 렌더 → page.evaluate

    기대하는 config 모양:
        {
            "requires_render": false | true,
            "source": {
                # requires_render=false
                "script_selector": "script#__NEXT_DATA__",
                # requires_render=true
                "list_url": "...", "list_params": {...}, "state_source": "window.__NUXT__",
                "wait_for_ms": 4000,      # 선택
                "item_path": "props.pageProps.jobs",
            }
        }

    통과 조건:
        1. state 획득 (script 또는 렌더)
        2. 내부가 유효 구조 (dict/list) 로 파싱됨
        3. item_path 로 도달한 값이 list 이고 길이 >= MIN_LIST_ROWS
        4. 아이템 중 MIN_TITLE_MATCH_RATIO 이상이 제목성 필드를 가짐
    """
    source = config.get("source") or {}
    item_path = source.get("item_path", "")
    requires_render = bool(config.get("requires_render"))

    if requires_render:
        if not url:
            return ValidationReport(
                ok=False,
                reason="requires_render=True 지만 validator 에 url 미제공",
            )
        state_source = source.get("state_source")
        if not state_source:
            return ValidationReport(
                ok=False,
                reason="requires_render=True 지만 source.state_source 비어있음",
            )
        try:
            state = _fetch_state_via_render(url, source)
        except Exception as e:
            return ValidationReport(
                ok=False,
                reason=f"렌더 state 추출 실패: {type(e).__name__}: {e}",
            )
        if state is None:
            return ValidationReport(
                ok=False,
                reason=f"page.evaluate({state_source!r}) 결과 None",
            )
        if not isinstance(state, (dict, list)):
            return ValidationReport(
                ok=False,
                reason=f"state 타입이 dict/list 아님 (type={type(state).__name__})",
            )
    else:
        script_selector = source.get("script_selector")
        if not script_selector:
            return ValidationReport(ok=False, reason="source.script_selector 가 비어있음")

        try:
            soup = BeautifulSoup(html, "lxml")
        except Exception as e:
            return ValidationReport(ok=False, reason=f"HTML 파싱 실패: {type(e).__name__}: {e}")

        tag = soup.select_one(script_selector)
        if not tag or not tag.string:
            return ValidationReport(
                ok=False,
                reason=f"script_selector {script_selector!r} 으로 스크립트/내용 없음",
            )

        try:
            state = json.loads(tag.string.strip())
        except json.JSONDecodeError as e:
            return ValidationReport(
                ok=False,
                reason=f"스크립트 내용 JSON 파싱 실패: {e}",
            )

    items = _traverse_path(state, item_path)
    if items is None:
        return ValidationReport(
            ok=False,
            reason=f"item_path {item_path!r} 경로 도달 실패 (중간 키 누락)",
        )

    if not isinstance(items, list):
        return ValidationReport(
            ok=False,
            reason=f"item_path 끝 값이 list 아님 (type={type(items).__name__})",
        )

    if len(items) < MIN_LIST_ROWS:
        return ValidationReport(
            ok=False,
            reason=f"배열 길이 {len(items)} < {MIN_LIST_ROWS}",
            items_extracted=len(items),
        )

    # 제목성 필드 추출 비율 검사
    titles: list = []
    matched = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        picked = _pick_title(item)
        if picked and len(picked) >= MIN_TITLE_LEN:
            titles.append(picked)
            matched += 1

    fields_matched = {"title": matched}
    match_ratio = matched / len(items)
    if match_ratio < MIN_TITLE_MATCH_RATIO:
        return ValidationReport(
            ok=False,
            reason=(
                f"제목성 필드 추출 비율 {match_ratio:.0%} ({matched}/{len(items)}) "
                f"< {MIN_TITLE_MATCH_RATIO:.0%}"
            ),
            items_extracted=len(items),
            fields_matched=fields_matched,
            sample_titles=titles[:3],
        )

    if _all_ui_noise(titles):
        return ValidationReport(
            ok=False,
            reason="추출된 제목이 모두 UI/네비 키워드 — item_path 가 네비/필터 배열일 가능성",
            items_extracted=len(items),
            fields_matched=fields_matched,
            sample_titles=titles[:3],
        )

    return ValidationReport(
        ok=True,
        items_extracted=len(items),
        fields_matched=fields_matched,
        sample_titles=titles[:3],
    )


def _traverse_path(state: Any, path: str) -> Any:
    """dot-notation path 로 state 내부 값에 도달. 실패 시 None.

    [*] 와일드카드: `data.outer[*].inner` → outer 배열의 각 아이템에서 inner 를 꺼내 flatten.
    벼룩시장(findall) 처럼 `data.partTimeJobList[0..].jobAdList` 같은 2단계 중첩 공고 리스트
    대응. inner 가 list 면 extend, scalar/dict 면 append.
    """
    if not path:
        return state
    if "[*]" in path:
        before, _, after = path.partition("[*].")
        if not after:
            # 잘못된 구문 (`path[*]` 뒤에 아무 것도 없음)
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


VALIDATOR_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
VALIDATOR_RENDER_NAV_TIMEOUT_MS = 30_000
VALIDATOR_RENDER_WAIT_MS_DEFAULT = 4000


def _fetch_state_via_render(url: str, source: dict) -> Any:
    """
    Playwright 로 url 열고 source.state_source 평가해 state 반환.
    embedded_crawler._extract_state_via_render 와 거의 동일하지만,
    validator 는 cmd_add 시점이라 list_url/params 재조립 없이 url 그대로 씀.
    """
    from playwright.sync_api import sync_playwright

    state_source = source["state_source"]
    wait_ms = int(source.get("wait_for_ms") or VALIDATOR_RENDER_WAIT_MS_DEFAULT)

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(
                user_agent=VALIDATOR_USER_AGENT,
                ignore_https_errors=True,
            )
            page = context.new_page()
            try:
                page.goto(
                    url,
                    wait_until="load",
                    timeout=VALIDATOR_RENDER_NAV_TIMEOUT_MS,
                )
            except Exception:
                pass
            page.wait_for_timeout(wait_ms)
            return page.evaluate(f"() => {state_source}")
        finally:
            browser.close()


def _pick_title(item: dict, _depth: int = 0) -> str:
    """아이템 dict 에서 제목 후보 필드 첫 매치 반환.

    2가지 nested 패턴 대응:
      1. `{"title": {"text": "..."}}` — title 값이 dict 인 경우 text/name/value 탐색
      2. `{"node": {...}, ...}` / `{"data": {...}}` 같은 wrapper — 라인 edges[*].node,
         Gatsby/Strapi 류 공통 패턴. 바로 title 못 찾으면 wrapper 안쪽으로 1단계 recurse.
    """
    for key in EMBEDDED_TITLE_KEYS:
        if key in item:
            v = item[key]
            if isinstance(v, str):
                return v.strip()
            if isinstance(v, dict):
                for inner in ("text", "name", "value"):
                    if inner in v and isinstance(v[inner], str):
                        return v[inner].strip()

    # wrapper 1단계 unwrap (라인 node, Strapi attributes 등). 재귀 깊이 1로 제한.
    if _depth < 1:
        for wrapper in TITLE_WRAPPER_KEYS:
            if wrapper in item and isinstance(item[wrapper], dict):
                inner = _pick_title(item[wrapper], _depth=_depth + 1)
                if inner:
                    return inner
    return ""
