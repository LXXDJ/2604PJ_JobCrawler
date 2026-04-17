"""
LLM 기반 사이트 분석 전략 (스텁)

Claude API를 호출해서 HTML을 분석하고 크롤러 설정을 생성한다.
현재는 인터페이스만 정의되어 있고 실제 구현은 비활성화 상태.

활성화 방법:
1. main.py에서 USE_LLM = True
2. ANTHROPIC_API_KEY 환경변수 설정
3. 이 파일의 analyze() 메서드 구현

휴리스틱이 실패한 경우 (confidence < 임계값)의 폴백으로 사용된다.
"""

from typing import Optional
from ..models import AnalysisResult, SiteType
from .base import AnalysisStrategy


class LLMStrategy(AnalysisStrategy):
    """
    LLM(Claude)을 사용한 사이트 분석.

    장점: 휴리스틱이 놓치는 케이스 커버
    단점: API 비용, 속도 느림, 비결정적
    사용 시점: 휴리스틱 실패 시 폴백
    """

    def __init__(
        self,
        enabled: bool = False,
        api_key: Optional[str] = None,
        model: str = "claude-sonnet-4-6",
    ):
        super().__init__(enabled=enabled, name="llm")
        self.api_key = api_key
        self.model = model

    def analyze(self, url: str, html: str = None) -> Optional[AnalysisResult]:
        """
        LLM을 호출해서 사이트 분석.

        TODO: 구현 예정 (Phase 2)
        - HTML 일부를 Claude에게 전달
        - "이 사이트의 플랫폼 타입과 게시글 목록/상세 페이지의 CSS 셀렉터를 알려줘" 요청
        - 응답을 AnalysisResult로 변환
        """
        if not self.api_key:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes="LLM strategy enabled but api_key not provided",
            )

        # 실제 구현 자리
        raise NotImplementedError(
            "LLMStrategy is not yet implemented. "
            "Set USE_LLM = False in main.py or implement this method."
        )
