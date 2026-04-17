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
)


# heuristic 이 이 타입 중 하나를 반환하면 Playwright 로 API 발견 시도
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
          - heuristic 이 유효하고 SPA 아님 → 그 결과 바로 반환
          - heuristic 유효하지 않음 (예: static_html with low confidence)
            → LLM 폴백
        """
        last_result = None

        for strategy in self.strategies:
            if not strategy.enabled:
                continue

            # Playwright 는 직전 결과가 SPA 인 경우만 돌림 (브라우저 띄우는 비용 크므로)
            if strategy.name == "playwright_discovery":
                if last_result is None or last_result.site_type not in SPA_TYPES:
                    continue

            # LLM 은 heuristic 이 SPA 를 고신뢰로 잡았으면 스킵
            # (SPA HTML 은 빈 껍데기라 LLM 도 의미 있는 selectors 못 뽑음)
            if strategy.name == "llm":
                if last_result is not None and last_result.site_type in SPA_TYPES:
                    continue

            result = strategy.run(url)
            if result is None:
                continue

            last_result = result

            # SPA heuristic 결과는 short-circuit 하지 않음 — playwright 에게 기회 주기
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
