"""
사이트 분석 모듈

URL을 받아서 사이트 타입을 분류하고, 해당 타입에 맞는
크롤러 설정(config)을 자동으로 생성한다.

사용 예:
    from analyzer import SiteAnalyzer

    analyzer = SiteAnalyzer()
    result = analyzer.analyze("https://www.camhr.com/a/job")
    # result.site_type = "spa_nuxt"
    # result.config = { "list_api": "...", "detail_api": "..." }
"""

from .analyzer import SiteAnalyzer
from .models import AnalysisResult, SiteType

__all__ = ["SiteAnalyzer", "AnalysisResult", "SiteType"]
