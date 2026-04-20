"""
config 검증기 — LLM 환각 방어선

analyzer 가 생성한 config 를 sites.json 에 저장하기 전에, 실제로 돌려봤을 때
의미 있는 데이터가 나오는지 확인한다.

이게 통과하지 못한 config 는 `validated=true` 가 붙지 않고, sites_registry 가
저장을 거부한다. 덕분에 LLM 이 엉뚱한 selectors 를 제안해도 "프로덕션에서
0건 수집" 같은 침묵 실패를 방지할 수 있다.

Phase 1 범위:
    - validate_dom_config : 실제 구현
    - validate_api_config / validate_embedded_json : stub (Phase 2/3)
"""

from dataclasses import dataclass, field
from typing import Optional

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
# API config 검증 (Phase 3 에서 구현)
# ============================================================

def validate_api_config(config: dict) -> ValidationReport:
    """
    API 엔드포인트를 실제로 호출해서 응답이 공고 리스트 형태인지 확인.
    Phase 3 에서 실제 구현 예정. 지금은 stub — 항상 ok=True (기존 분석기 동작 유지).
    """
    return ValidationReport(
        ok=True,
        reason="validate_api_config: Phase 3 에서 실제 구현 예정 (현재 stub)",
    )


# ============================================================
# Embedded JSON config 검증 (Phase 2 에서 구현)
# ============================================================

def validate_embedded_json_config(html: str, config: dict) -> ValidationReport:
    """
    HTML 내 <script> 에서 state JSON 뽑고 item_path 로 배열 도달 가능한지 확인.
    Phase 2 에서 실제 구현 예정. 지금은 stub.
    """
    return ValidationReport(
        ok=False,
        reason="validate_embedded_json_config: Phase 2 에서 실제 구현 예정 (현재 stub)",
    )
