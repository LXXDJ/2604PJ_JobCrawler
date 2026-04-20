"""
LLM 기반 사이트 분석 전략

OpenAI API 에 HTML 을 전달해서 "이거 무슨 플랫폼이고 CSS 셀렉터는 뭐야?" 를 묻는다.
휴리스틱이 실패한 경우(confidence < min_confidence)의 폴백으로 사용.

활성화:
1. main.py 에서 USE_LLM = True
2. .env (또는 OS 환경변수)에 OPENAI_API_KEY 설정
"""

import json
import re
from typing import Optional

import requests

from ..models import AnalysisResult, SiteType
from .base import AnalysisStrategy


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# LLM 에게 보낼 excerpt 최대 길이 (정리 후 기준).
# gpt-4o-mini 기준 150k chars ≈ 40~60k tokens → 건당 약 $0.008. 정리 후에도 JobKorea 급
# 대형 사이트는 목록 DOM 이 100k+ offset 에 있어서 이 정도 필요.
# 1회성 1500 사이트 분석 비용 ≈ $12 — 허용범위.
MAX_HTML_CHARS = 150000

# 속성 하나의 최대 길이. 지나치게 큰 data-* 속성(예: JSON 덤프)은 이 길이로 잘라
# 선택자 추출용 DOM 구조가 excerpt 안에 들어올 수 있도록 한다.
MAX_ATTR_VALUE_CHARS = 200

# script/style/noscript 태그는 대부분의 사이트에서 excerpt 앞부분을 독점해
# 실제 DOM 구조(게시판 행, 목록 컨테이너 등) 가 LLM 에게 안 보이는 문제를 일으킨다.
# 예: JobKorea `/recruit/joblist` 는 첫 560KB 가 tracking/분석 JS, 실제 `tr.devloopArea`
# 는 offset 568k 에 등장 — 20k excerpt 로는 절대 안 잡힘.
# → excerpt 만들기 전에 <script>/<style>/<noscript>/주석 제거.
_CLEAN_STRIP_PATTERNS = [
    re.compile(r"<script\b[^>]*>.*?</script\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<style\b[^>]*>.*?</style\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<noscript\b[^>]*>.*?</noscript\s*>", re.IGNORECASE | re.DOTALL),
    re.compile(r"<!--.*?-->", re.DOTALL),
]

# 따옴표로 둘러싸인 속성값: attr="..." 또는 attr='...'
_ATTR_VALUE_PATTERN = re.compile(r"""(\s[a-zA-Z_:][\w:.-]*\s*=\s*)("([^"]*)"|'([^']*)')""")


def _truncate_large_attributes(html: str, max_attr_chars: int = MAX_ATTR_VALUE_CHARS) -> str:
    """
    긴 속성값(보통 inline JSON in data-* 같은 경우) 을 자른다.
    JobKorea 같이 지역 필터가 data-districts='[{...huge JSON...}]' 형태로
    수십 KB 씩 점유하는 경우, DOM 구조가 excerpt 끝으로 밀려나 LLM 이 못 본다.
    """
    def _replace(m: re.Match) -> str:
        prefix = m.group(1)  # " attr="
        quote = m.group(2)[0]  # " 또는 '
        value = m.group(3) if m.group(3) is not None else m.group(4)
        if len(value) <= max_attr_chars:
            return m.group(0)
        truncated = value[:max_attr_chars] + "...[truncated]"
        return f"{prefix}{quote}{truncated}{quote}"

    return _ATTR_VALUE_PATTERN.sub(_replace, html)


def _collapse_whitespace(html: str) -> str:
    """태그 사이 공백/개행 축소 — excerpt 에 더 많은 DOM 을 담는다."""
    # 연속 공백/탭/개행을 공백 하나로
    return re.sub(r"\s+", " ", html)


def _find_list_dense_window(
    html: str, window_size: int, scan_from: int = 0
) -> Optional[tuple[int, int]]:
    """
    HTML 에서 '목록 DOM 이 집중된 구간' 을 찾는다.

    우선순위:
      1) <tbody> 중 다음 3000자 안에 <tr> 5개 이상 + <a href=> 있는 곳 (테이블 목록)
      2) 같은 (tag, class) 쌍이 10회 이상 반복되고 그 구간에 <a href=> 있는 곳
         → <a href=> 가 없으면 필터/체크박스 리스트일 확률이 높아 제외

    Returns: (start, end) index 튜플, 없으면 None
    """
    # 1) <tbody> 후보 — 테이블 기반 목록 사이트 (JobKorea, 그누보드 일부 등)
    for tb in re.finditer(r"<tbody\b", html[scan_from:], re.IGNORECASE):
        pos = tb.start() + scan_from
        window_head = html[pos : pos + 3000]
        tr_count = len(re.findall(r"<tr\b", window_head, re.IGNORECASE))
        has_anchor = "<a " in window_head.lower() and "href=" in window_head.lower()
        if tr_count >= 5 and has_anchor:
            start = max(0, pos - 500)
            return (start, min(len(html), start + window_size))

    # 2) 반복 (tag, class) 쌍 — 단, 클러스터 내 <a href=> 요구
    tag_class_positions: dict[str, list[int]] = {}
    for m in re.finditer(
        r'<(tr|li|div|article|section)\b[^>]*class\s*=\s*"([^"]{1,120})"',
        html[scan_from:],
        re.IGNORECASE,
    ):
        key = f'{m.group(1).lower()}.{m.group(2)}'
        tag_class_positions.setdefault(key, []).append(m.start() + scan_from)

    candidates = []
    for key, positions in tag_class_positions.items():
        if len(positions) < 10:
            continue
        # 클러스터 범위
        cluster_start = positions[0]
        cluster_end = min(len(html), positions[min(len(positions) - 1, 9)] + 2000)
        cluster_text = html[cluster_start:cluster_end].lower()
        if "<a " in cluster_text and "href=" in cluster_text:
            candidates.append((key, cluster_start))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[1])
    _, earliest = candidates[0]
    start = max(0, earliest - 500)
    return (start, min(len(html), start + window_size))


def _build_llm_excerpt(html: str, max_chars: int = MAX_HTML_CHARS) -> str:
    """
    LLM 에게 보낼 HTML excerpt 를 만든다.

    전략:
      1) script/style/noscript/HTML 주석 제거 — DOM 구조에 관심 있는데 tracking JS 가
         앞부분을 가득 채우면 LLM 이 실제 선택자를 못 봄.
      2) 긴 속성값(>200 chars) 절단 — data-* JSON 덤프 같은 noise 로 실제 DOM 이 밀림.
      3) 공백 축소 — 불필요한 indent/개행 제거.
      4) 앞 절반 excerpt + 반복 구조 집중 구간 excerpt 결합 — 목록 DOM 이 뒤쪽에
         있어도 LLM 이 실제 셀렉터를 볼 수 있도록.

    Returns: 정리된 excerpt (max_chars 이내)
    """
    cleaned = html
    for pat in _CLEAN_STRIP_PATTERNS:
        cleaned = pat.sub("", cleaned)

    cleaned = _truncate_large_attributes(cleaned)
    cleaned = _collapse_whitespace(cleaned)

    # 정리 후 텍스트가 비정상적으로 짧으면 (파싱 오류/특이 구조) 원본 앞부분으로 폴백.
    # SPA 시그니처 (__NUXT__ 등) 는 script 안에 있어 원본에서만 보임 — 완전 폴백.
    if len(cleaned) < 500:
        return html[:max_chars]

    # 앞부분이 곧 목록일 때(짧은 페이지, 상단에 <tbody> 등)는 그대로 잘라 반환.
    if len(cleaned) <= max_chars:
        return cleaned

    head_size = max_chars // 3
    tail_budget = max_chars - head_size - 50  # 구분자 여유

    # head_size 이후에서 반복 구조 윈도우 찾기 — 앞부분에 이미 포함된 것 무시
    dense = _find_list_dense_window(cleaned, tail_budget, scan_from=head_size)

    if dense is None:
        # 반복 구조 못 찾음 — 그냥 앞부분 max_chars 로 반환
        return cleaned[:max_chars]

    start, end = dense
    return (
        cleaned[:head_size]
        + "\n<!-- ...[중간 생략]... -->\n"
        + cleaned[start:end]
    )


SYSTEM_PROMPT = """너는 게시판/구인구직 사이트의 HTML 을 보고 크롤러 설정을 만드는 분석기야.

응답은 반드시 JSON 객체 하나만. 마크다운 펜스나 추가 설명 없이 JSON 만.

스키마:
{
  "site_type": "gnuboard | spa_nuxt | spa_next | spa_vue | spa_react | wordpress | static_html | unknown",
  "confidence": 0.0 ~ 1.0,
  "reasoning": "한 줄 판단 근거",
  "config": {
    "platform": "site_type 와 동일",
    "base_url": "스킴+호스트 (예: https://example.com)",
    "board_table": "(gnuboard 전용) URL의 bo_table 값",
    "theme": "nariya | fz | custom | unknown",
    "parse_mode": "direct | sr_only",
    "selectors": {
      "list_rows": "게시글 목록 행 CSS 셀렉터",
      "subject_link": "제목 링크 (list_rows 내부)",
      "author": "작성자 (선택)",
      "date": "날짜 (선택)",
      "hit": "조회수 (선택)",
      "content": "상세 페이지 본문"
    }
  }
}

판단 시그니처:
- 'g5_bbs_url' / '/bbs/board.php' / 'g5_bo_table' → gnuboard
- '__NUXT__' / '/_nuxt/' → spa_nuxt
- '__NEXT_DATA__' / '/_next/' → spa_next
- 'wp-content/' / generator=WordPress → wordpress
- 위 시그니처 없는데 <table>/<ul> 안에 게시글 목록이 명시적으로 있음 → static_html
- 근거 약하면 unknown + 낮은 confidence

parse_mode 기준:
- 'sr_only': 제목 <a> 안에 <span class="sr-only">(스크린리더용) 텍스트가 따로 있는 테마
- 'direct': 그냥 <a> 텍스트가 제목

selectors 는 실제 HTML 구조를 보고 추출해. 알려진 시그니처가 없으면 theme="custom".
SPA(spa_nuxt/spa_next/…) 는 HTML 이 빈 껍데기라 selectors 채울 수 없음 — config 는 platform/base_url 만 넣어.
"""


class LLMStrategy(AnalysisStrategy):
    """
    LLM(OpenAI)을 사용한 사이트 분석.

    장점: 휴리스틱이 놓치는 커스텀 게시판/테마 커버
    단점: API 비용, 속도 느림(수 초), 비결정적
    사용 시점: 휴리스틱 실패 시 폴백
    """

    def __init__(
        self,
        enabled: bool = False,
        api_key: Optional[str] = None,
        model: str = "gpt-4o-mini",
        timeout: int = 60,
        max_tokens: int = 1024,
    ):
        super().__init__(enabled=enabled, name="llm")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.max_tokens = max_tokens

    def analyze(self, url: str, html: str = None) -> Optional[AnalysisResult]:
        if not self.api_key:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes="LLM 활성화됐지만 OPENAI_API_KEY 가 없음 (.env 확인)",
            )

        # 1) HTML 확보 — 휴리스틱이 먼저 돌며 html 을 캐시해줄 수 있지만,
        #    지금은 analyzer 가 캐시를 넘겨주지 않아 재수신 함. 한 번 더 fetch.
        if html is None:
            html, fetch_err = self._fetch_html(url)
            if html is None:
                return AnalysisResult(
                    url=url,
                    site_type=SiteType.UNKNOWN,
                    confidence=0.0,
                    strategy_name=self.name,
                    notes=f"HTML 가져오기 실패: {fetch_err}",
                )

        excerpt = _build_llm_excerpt(html, MAX_HTML_CHARS)

        # 2) OpenAI 호출
        try:
            raw = self._call_openai(url, excerpt)
        except Exception as e:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes=f"OpenAI API 호출 실패: {type(e).__name__}: {e}",
            )

        # 3) 응답 JSON 파싱
        parsed = self._extract_json(raw)
        if parsed is None:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes=f"LLM 응답 JSON 파싱 실패 (앞 200자): {raw[:200]!r}",
            )

        # 4) site_type 매핑 (모르는 값이면 UNKNOWN 으로 보정)
        try:
            site_type = SiteType(parsed.get("site_type", "unknown"))
        except ValueError:
            site_type = SiteType.UNKNOWN

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            confidence = 0.0
        confidence = max(0.0, min(1.0, confidence))

        config = parsed.get("config") or {}
        reasoning = parsed.get("reasoning", "")

        return AnalysisResult(
            url=url,
            site_type=site_type,
            confidence=confidence,
            config=config,
            strategy_name=self.name,
            notes=f"LLM: {reasoning}" if reasoning else "LLM 응답 (reasoning 없음)",
        )

    # --- 내부 헬퍼 ---

    def _fetch_html(self, url: str) -> tuple[Optional[str], str]:
        """단순 fetch — 휴리스틱이 이미 재시도했을 가능성이 크므로 여긴 1회만."""
        try:
            resp = requests.get(
                url,
                headers={"User-Agent": USER_AGENT},
                timeout=self.timeout,
                verify=False,
            )
            resp.raise_for_status()
            return resp.text, ""
        except Exception as e:
            return None, f"{type(e).__name__}: {e}"

    def _call_openai(self, url: str, html_excerpt: str) -> str:
        from openai import OpenAI

        client = OpenAI(api_key=self.api_key, timeout=self.timeout)
        response = client.chat.completions.create(
            model=self.model,
            max_tokens=self.max_tokens,
            # response_format 을 json_object 로 강제하면 항상 유효한 JSON 이 나옴
            # (system prompt 에 "JSON" 단어가 포함돼 있어야 함 — 위 SYSTEM_PROMPT 충족)
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"분석할 URL: {url}\n\n"
                        f"HTML (앞 {len(html_excerpt)}자):\n{html_excerpt}"
                    ),
                },
            ],
        )
        return response.choices[0].message.content or ""

    def _extract_json(self, text: str) -> Optional[dict]:
        """응답 문자열에서 JSON 객체를 꺼낸다. response_format=json_object 덕에 보통 그대로 파싱됨."""
        text = text.strip()

        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
        return None


# ============================================================
# Phase 2.7: validator 실패 피드백 기반 selectors 재제안
# ============================================================

RETRY_SYSTEM_PROMPT = """너는 이전에 제안한 CSS selectors 가 실제 HTML DOM 에서 매칭되지 않았다는 피드백을 받은 분석기야.
실패 사유와 HTML 을 다시 보고 selectors 를 수정해서 JSON 으로만 반환해.

중요 규칙:
- list_rows 는 '공고/게시글 하나당 하나씩 반복되는 컨테이너' CSS 셀렉터여야 해. 메뉴/필터/페이지네이션이 아님.
- subject_link 는 list_rows 컨테이너 **내부**에 있는 제목 링크 (<a> 또는 그 자손).
- HTML 에 실제로 존재하는 클래스/태그 조합만 사용 — 상상 금지. excerpt 안에서 찾을 수 있어야 해.
- 이전 실패 원인이 'list_rows 매칭 0개' 면 → 현재 selector 는 DOM 에 없음. 다른 클래스/구조를 찾아.
- 이전 실패 원인이 '제목 추출 비율 낮음' 이면 → list_rows 는 맞는데 subject_link 가 틀렸거나 너무 광범위함.
- 이전 실패 원인이 '헤더 필터 후 행 부족' 이면 → list_rows 가 너무 좁거나 전부 헤더 클래스가 있음.

반환 JSON (마크다운 펜스 없이):
{
  "list_rows": "...",
  "subject_link": "...",
  "author": "",
  "date": "",
  "hit": "",
  "content": "",
  "reasoning": "왜 이 selectors 로 바꿨는지 한 줄"
}

author/date/hit/content 는 확실한 경우만 채우고, 모르면 빈 문자열로 둬.
"""


def retry_dom_selectors(
    html: str,
    failed_selectors: dict,
    failure_reason: str,
    api_key: str,
    model: str = "gpt-4o-mini",
    timeout: int = 60,
    max_tokens: int = 512,
) -> tuple[Optional[dict], str]:
    """
    validator 가 거부한 DOM selectors 를 LLM 에 피드백과 함께 다시 제안받기.

    Args:
        html: validator 가 봤던 HTML (selectors 가 매칭될 대상)
        failed_selectors: 이전에 제안돼 실패한 selectors dict
        failure_reason: ValidationReport.reason 문자열

    Returns:
        (새 selectors dict, reasoning) 또는 (None, 실패 사유).
        selectors 는 최소한 list_rows / subject_link 가 non-empty 여야 반환.
    """
    if not api_key:
        return None, "api_key 없음"

    excerpt = _build_llm_excerpt(html, MAX_HTML_CHARS)

    user_message = (
        f"[이전 selectors]\n{json.dumps(failed_selectors, ensure_ascii=False, indent=2)}\n\n"
        f"[validator 실패 사유]\n{failure_reason}\n\n"
        f"[HTML 요약 ({len(excerpt)}자)]\n{excerpt}"
    )

    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=timeout)
        response = client.chat.completions.create(
            model=model,
            max_tokens=max_tokens,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": RETRY_SYSTEM_PROMPT},
                {"role": "user", "content": user_message},
            ],
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        return None, f"LLM 호출 실패: {type(e).__name__}: {e}"

    try:
        parsed = json.loads(raw.strip())
    except json.JSONDecodeError as e:
        return None, f"JSON 파싱 실패: {e} — raw: {raw[:200]!r}"

    # 최소 필수 필드 확인
    list_rows = parsed.get("list_rows") or ""
    subject_link = parsed.get("subject_link") or ""
    if not list_rows or not subject_link:
        return None, f"LLM 이 list_rows/subject_link 비워서 반환: {parsed!r}"

    # 이전과 완전히 같은 selectors 면 retry 무의미 — 실패 처리
    if (
        list_rows == failed_selectors.get("list_rows")
        and subject_link == failed_selectors.get("subject_link")
    ):
        return None, "LLM 이 이전과 동일한 selectors 반복"

    selectors = {
        "list_rows": list_rows,
        "subject_link": subject_link,
        "author": parsed.get("author") or "",
        "date": parsed.get("date") or "",
        "hit": parsed.get("hit") or "",
        "content": parsed.get("content") or "",
    }
    reasoning = str(parsed.get("reasoning", ""))[:300]
    return selectors, reasoning
