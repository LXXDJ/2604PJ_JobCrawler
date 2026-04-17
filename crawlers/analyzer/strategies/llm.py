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

# 토큰 절약용 — HTML 앞부분만 LLM 에 보냄.
# 보통 <head> + 상단 콘텐츠 + 스크립트 시그니처 식별에 충분.
MAX_HTML_CHARS = 20000


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

        excerpt = html[:MAX_HTML_CHARS]

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
