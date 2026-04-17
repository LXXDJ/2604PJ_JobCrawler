"""
분석 결과 데이터 클래스

사이트 분석 결과를 담는 표준 형식.
어느 전략(휴리스틱/LLM)을 쓰든 동일한 형식으로 반환한다.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any


class SiteType(Enum):
    """사이트 플랫폼 타입"""
    GNUBOARD = "gnuboard"          # 그누보드 기반 게시판 (한인회, 시엠립 등)
    SPA_NUXT = "spa_nuxt"          # Nuxt.js 기반 SPA (CamHR 등)
    SPA_NEXT = "spa_next"          # Next.js 기반 SPA
    SPA_VUE = "spa_vue"            # Vue.js SPA (Nuxt 외)
    SPA_REACT = "spa_react"        # React SPA
    WORDPRESS = "wordpress"        # WordPress
    STATIC_HTML = "static_html"    # 단순 정적 HTML
    API_DISCOVERED = "api_discovered"  # SPA 내부 API 가 Playwright 로 발견된 상태 — 수동 어댑터 필요
    UNKNOWN = "unknown"            # 식별 실패


@dataclass
class AnalysisResult:
    """사이트 분석 결과"""
    url: str
    site_type: SiteType
    confidence: float                         # 0.0 ~ 1.0 (결과 신뢰도)
    config: dict = field(default_factory=dict)  # 생성된 크롤러 설정
    strategy_name: str = ""                   # 어느 전략이 생성했는지
    notes: str = ""                           # 추가 정보/디버그용

    def is_valid(self, min_confidence: float = 0.5) -> bool:
        """사용 가능한 결과인지 판단"""
        return (
            self.site_type != SiteType.UNKNOWN
            and self.confidence >= min_confidence
            and bool(self.config)
        )

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "site_type": self.site_type.value,
            "confidence": self.confidence,
            "config": self.config,
            "strategy_name": self.strategy_name,
            "notes": self.notes,
        }
