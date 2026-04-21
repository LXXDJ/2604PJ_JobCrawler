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

# DOM selectors 가 필수인 site_type 들.
# heuristic 이 이 타입을 자신 있게 잡아도 (confidence ≥ min) selectors 가 비어있으면
# 그대로 반환하면 can_register 에서 거부되므로, LLM 으로 selectors 만 보충해야 한다.
# (예: radiokorea — /bbs/board.php 시그니처로 gnuboard 확정했지만 테마를 na-table/fz 중
#  어디에도 못 맞춘 케이스)
DOM_SELECTOR_REQUIRED_TYPES = {
    SiteType.GNUBOARD,
    SiteType.STATIC_HTML,
    SiteType.WORDPRESS,
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
          - LLM 이 뒤늦게 SPA 판정 (heuristic 에선 놓침) → playwright_discovery
            를 retroactively 재호출 (post-hoc SPA recovery)
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
                # Phase 2.8: heuristic 이 DOM 타입을 확정했지만 selectors 가 비어있으면
                # 그대로 반환하면 can_register 에서 거부됨 — LLM 으로 selectors 만 보충.
                if (
                    strategy.name == "heuristic"
                    and _needs_selector_enrichment(result)
                ):
                    enriched = self._enrich_with_llm_selectors(url, result)
                    if enriched is not None:
                        return enriched

                # Post-hoc SPA recovery — LLM (또는 embedded_json) 이 뒤늦게 SPA 로 판정했지만
                # heuristic 이 SPA 마커를 못 잡아서 playwright_discovery 가 스킵된 경우. 예:
                # 스카우트 는 초기 HTML 빈 껍데기지만 `window.__NUXT__=`/`/_nuxt/` 마커 없어
                # heuristic 이 SPA 못 잡음. LLM 이 "HTML 빈 껍데기 = SPA" 라고 추측한 결과를
                # 버리지 않고 playwright_discovery 에 한 번 더 기회를 준다.
                if (
                    result.site_type in SPA_TYPES
                    and heuristic_type not in SPA_TYPES
                    and strategy.name != "playwright_discovery"
                ):
                    recovered = self._recover_spa_with_playwright(url)
                    if recovered is not None and recovered.is_valid(self.min_confidence):
                        return recovered

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

    def _recover_spa_with_playwright(self, url: str) -> Optional[AnalysisResult]:
        """LLM 이 SPA 라고 뒤늦게 판정한 경우 playwright_discovery 를 재호출.

        루프 안에서는 heuristic_type in SPA_TYPES 조건 때문에 스킵됐지만,
        LLM 이 "빈 HTML = SPA" 라고 추측했다면 그 추측을 믿고 실제 네트워크 캡처를 시도.
        스카우트·벼룩시장 같이 명시 SPA 마커가 없는 사이트에 유효.
        """
        playwright = next(
            (s for s in self.strategies
             if s.name == "playwright_discovery" and s.enabled),
            None,
        )
        if playwright is None:
            return None
        try:
            return playwright.run(url)
        except Exception:
            # playwright 자체 예외는 삼킴 — 그냥 recovery 실패로 처리
            return None

    def _enrich_with_llm_selectors(
        self, url: str, heuristic_result: AnalysisResult
    ) -> Optional[AnalysisResult]:
        """
        heuristic 이 확정한 site_type 을 그대로 두고, LLM 을 별도 호출해 selectors 만 보충.

        예: radiokorea 에서 heuristic 이 "/bbs/board.php" 시그니처로 gnuboard 확정하지만
        테마(na-table/fz)를 못 맞춰 selectors={} 로 반환한 경우. 여기서 LLM 에 던지면
        실제 HTML 구조 기반 selectors 를 받아 병합할 수 있다.

        성공 시 병합된 AnalysisResult, LLM 이 없거나 유효 selectors 못 받으면 None.
        """
        llm_strategy = next(
            (s for s in self.strategies if s.name == "llm" and s.enabled),
            None,
        )
        if llm_strategy is None:
            return None

        llm_result = llm_strategy.run(url)
        if llm_result is None:
            return None

        llm_selectors = (llm_result.config or {}).get("selectors") or {}
        if not llm_selectors.get("list_rows") or not llm_selectors.get("subject_link"):
            return None

        # heuristic 기반 config 에 LLM 의 selectors/theme/parse_mode 만 덮어쓰기.
        merged_config = dict(heuristic_result.config or {})
        merged_config["selectors"] = llm_selectors
        for k in ("theme", "parse_mode"):
            v = (llm_result.config or {}).get(k)
            if v:
                merged_config[k] = v

        merged_notes = (
            f"{heuristic_result.notes} + LLM 으로 selectors 보충"
            if heuristic_result.notes
            else "LLM 으로 selectors 보충"
        )

        return AnalysisResult(
            url=url,
            site_type=heuristic_result.site_type,
            confidence=heuristic_result.confidence,
            config=merged_config,
            strategy_name=f"{heuristic_result.strategy_name}+llm_selectors",
            notes=merged_notes,
        )


def _needs_selector_enrichment(result: AnalysisResult) -> bool:
    """DOM selectors 가 필요한 타입인데 list_rows/subject_link 가 비어있으면 True."""
    if result.site_type not in DOM_SELECTOR_REQUIRED_TYPES:
        return False
    selectors = (result.config or {}).get("selectors") or {}
    return not selectors.get("list_rows") or not selectors.get("subject_link")
