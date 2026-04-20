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

    try:
        if method == "GET":
            response = requests.get(
                api_endpoint,
                params=list_params,
                headers=headers,
                timeout=VALIDATOR_API_TIMEOUT,
            )
        elif method == "POST":
            response = requests.post(
                api_endpoint,
                json=list_params,
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

    return ValidationReport(
        ok=True,
        items_extracted=len(items),
        fields_matched=fields_matched,
        sample_titles=titles[:3],
    )


# ============================================================
# Embedded JSON config 검증
# ============================================================

# 공고 아이템 내에서 "제목성" 필드로 간주할 key 후보.
# embedded_crawler._pick_field 와 기본 의도는 동일하지만, 여기선 validator 만의
# 책임 (실제 배열에 제목이 있는가) 을 위해 독립적으로 정의해둔다.
EMBEDDED_TITLE_KEYS = [
    "title", "jobTitle", "postSubject", "subject", "name",
    "jobName", "jobPostingName", "position", "positionName",
]


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
    """dot-notation path 로 state 내부 값에 도달. 실패 시 None."""
    if not path:
        return state
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


def _pick_title(item: dict) -> str:
    """아이템 dict 에서 제목 후보 필드 첫 매치 반환."""
    for key in EMBEDDED_TITLE_KEYS:
        if key in item:
            v = item[key]
            if isinstance(v, str):
                return v.strip()
            # nested dict (예: {"title": {"text": "..."}}) — 흔한 몇 가지만 시도
            if isinstance(v, dict):
                for inner in ("text", "name", "value"):
                    if inner in v and isinstance(v[inner], str):
                        return v[inner].strip()
    return ""
