"""
SiteAnalyzer — 분석 전략들을 오케스트레이션

여러 전략을 순서대로 실행해서 가장 먼저 유효한 결과를 반환한다.
휴리스틱 → LLM 순서로 시도하는 게 일반적.
"""

from typing import Optional, List
from .models import AnalysisResult, SiteType
from .strategies import AnalysisStrategy, HeuristicStrategy, LLMStrategy


class SiteAnalyzer:
    """
    사이트 분석 오케스트레이터.

    전략 목록을 받아서 순서대로 실행하고,
    유효한 결과(is_valid=True)가 나오면 그걸 반환한다.
    """

    def __init__(
        self,
        use_llm: bool = False,
        llm_api_key: Optional[str] = None,
        llm_model: str = "claude-sonnet-4-6",
        min_confidence: float = 0.5,
        strategies: Optional[List[AnalysisStrategy]] = None,
    ):
        """
        use_llm: LLM 전략 활성화 여부 (기본 False)
        llm_api_key: LLM API 키 (use_llm=True일 때 필요)
        llm_model: 사용할 Claude 모델
        min_confidence: 결과를 유효로 판단할 최소 신뢰도
        strategies: 커스텀 전략 목록 (미지정 시 기본 구성 사용)
        """
        self.min_confidence = min_confidence

        if strategies is not None:
            self.strategies = strategies
        else:
            # 기본 구성: 휴리스틱 → LLM
            self.strategies = [
                HeuristicStrategy(enabled=True),
                LLMStrategy(
                    enabled=use_llm,
                    api_key=llm_api_key,
                    model=llm_model,
                ),
            ]

    def analyze(self, url: str) -> AnalysisResult:
        """
        URL을 분석해서 가장 신뢰도 높은 결과를 반환.
        모든 전략이 실패하면 마지막 시도 결과(또는 UNKNOWN)를 반환한다.
        """
        last_result = None
        html_cache = None  # HTML 재사용용

        for strategy in self.strategies:
            if not strategy.enabled:
                continue

            result = strategy.run(url, html=html_cache)
            if result is None:
                continue

            last_result = result

            # 유효하면 바로 반환
            if result.is_valid(self.min_confidence):
                return result

            # 유효하지 않으면 HTML만 캐시하고 다음 전략 시도
            # (아직 캐시 방식은 간단히 — 나중에 개선)

        # 모든 전략 실패
        if last_result:
            return last_result

        return AnalysisResult(
            url=url,
            site_type=SiteType.UNKNOWN,
            confidence=0.0,
            strategy_name="none",
            notes="모든 전략이 None을 반환했거나 비활성화됨",
        )
