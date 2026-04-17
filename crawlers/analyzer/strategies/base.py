"""
분석 전략 인터페이스 (Strategy Pattern)

모든 분석 전략(HeuristicStrategy, LLMStrategy 등)은
이 인터페이스를 구현해야 한다.

enabled=False 로 설정된 전략은 실행 시 None을 반환하고 스킵된다.
→ 이 구조 덕분에 LLM 전략을 코드에 두고도 쉽게 끄고 켤 수 있음.
"""

from abc import ABC, abstractmethod
from typing import Optional
from ..models import AnalysisResult


class AnalysisStrategy(ABC):
    """분석 전략 추상 클래스"""

    def __init__(self, enabled: bool = True, name: str = ""):
        self.enabled = enabled
        self.name = name or self.__class__.__name__

    def run(self, url: str, html: str = None) -> Optional[AnalysisResult]:
        """
        전략 실행. enabled=False면 None을 반환하고 스킵된다.

        url: 분석 대상 URL
        html: 이미 가져온 HTML (재사용용, 없으면 내부에서 가져옴)
        """
        if not self.enabled:
            return None
        return self.analyze(url, html)

    @abstractmethod
    def analyze(self, url: str, html: str = None) -> Optional[AnalysisResult]:
        """실제 분석 로직 (서브클래스 구현)"""
        ...
