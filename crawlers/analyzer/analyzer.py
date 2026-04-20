"""
SiteAnalyzer — 분석 전략들을 오케스트레이션

여러 전략을 순서대로 실행해서 가장 먼저 유효한 결과를 반환한다.
휴리스틱 → LLM 순서로 시도하는 게 일반적.
"""

from typing import Optional, List
from .models import AnalysisResult, SiteType
from .strategies import (
    AnalysisStrategy,
    HeuristicStrategy,
    LLMStrategy,
    PlaywrightDiscoveryStrategy,
    EmbeddedJSONStrategy,
)


# heuristic 이 이 타입 중 하나를 반환하면 SPA-전용 전략 (playwright / embedded_json) 호출
SPA_TYPES = {
    SiteType.SPA_NUXT,
    SiteType.SPA_NEXT,
    SiteType.SPA_VUE,
    SiteType.SPA_REACT,
}


class SiteAnalyzer:
    """
    사이트 분석 오케스트레이터.

    전략 순서: heuristic → (SPA 면) playwright_discovery → (아니면) LLM
    유효한 결과가 나오면 그 시점에 반환한다.
    """

    def __init__(
        self,
        use_llm: bool = False,
        llm_api_key: Optional[str] = None,
        llm_model: str = "gpt-4o-mini",
        use_playwright: bool = True,
        min_confidence: float = 0.5,
        strategies: Optional[List[AnalysisStrategy]] = None,
    ):
        """
        use_llm: LLM 전략 활성화 여부 (기본 False)
        llm_api_key: LLM API 키 (use_llm=True일 때 필요)
        llm_model: 사용할 LLM 모델 (OpenAI)
        use_playwright: SPA 감지 시 Playwright 로 API 엔드포인트 자동 발견 (기본 True)
        min_confidence: 결과를 유효로 판단할 최소 신뢰도
        strategies: 커스텀 전략 목록 (미지정 시 기본 구성 사용)
        """
        self.min_confidence = min_confidence

        if strategies is not None:
            self.strategies = strategies
        else:
            self.strategies = [
                HeuristicStrategy(enabled=True),
                PlaywrightDiscoveryStrategy(enabled=use_playwright),
                EmbeddedJSONStrategy(
                    enabled=True,
                    use_llm=use_llm,
                    llm_api_key=llm_api_key,
                    llm_model=llm_model,
                ),
                LLMStrategy(
                    enabled=use_llm,
                    api_key=llm_api_key,
                    model=llm_model,
                ),
            ]

    def analyze(self, url: str) -> AnalysisResult:
        """
        URL 을 분석해서 가장 신뢰도 높은 결과를 반환.

        라우팅 규칙:
          - heuristic 이 SPA 로 판정 → playwright_discovery 실행 (가능하면)
            → 실패하면 embedded_json 시도 (__NEXT_DATA__ 같은 SSR state 추출)
            → 모두 실패 시 LLM 은 스킵 (SPA HTML 은 빈 껍데기라 의미 없음)
          - heuristic 이 SPA 아니고 유효 → 그 결과 바로 반환
          - heuristic 유효하지 않음 (예: static_html with low confidence) → LLM 폴백
        """
        last_result: Optional[AnalysisResult] = None
        heuristic_type: Optional[SiteType] = None

        for strategy in self.strategies:
            if not strategy.enabled:
                continue

            # SPA 전용 전략들 (playwright / embedded_json) — heuristic 이 SPA 판정한 경우만 실행
            if strategy.name in ("playwright_discovery", "embedded_json"):
                if heuristic_type not in SPA_TYPES:
                    continue
                # 직전 전략이 이미 유효한 결과를 냈으면 (예: playwright 가 API 찾음) 루프 안에서
                # 이미 return 했을 것 — 여기 도달했다는 건 앞선 SPA 시도가 실패했다는 의미.

            # LLM 은 SPA 에선 스킵 (HTML 이 빈 껍데기라 의미 있는 selectors 못 뽑음)
            if strategy.name == "llm":
                if heuristic_type in SPA_TYPES:
                    continue

            result = strategy.run(url)
            if result is None:
                continue

            # heuristic 결과를 기억해서 이후 SPA 전용 분기 라우팅에 사용
            if strategy.name == "heuristic":
                heuristic_type = result.site_type

            last_result = result

            # SPA heuristic 결과는 short-circuit 하지 않음 — playwright/embedded_json 에 기회
            if result.site_type in SPA_TYPES and result.config.get("needs_playwright_discovery"):
                continue

            if result.is_valid(self.min_confidence):
                return result

        if last_result:
            return last_result

        return AnalysisResult(
            url=url,
            site_type=SiteType.UNKNOWN,
            confidence=0.0,
            strategy_name="none",
            notes="모든 전략이 None 을 반환했거나 비활성화됨",
        )
