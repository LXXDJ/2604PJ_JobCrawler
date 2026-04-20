"""_gnuboard_analysis_to_source 가 입력 URL 을 올바르게 보존하는지 확인."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, "crawlers")

from analyzer.models import AnalysisResult, SiteType
from sites_registry import _gnuboard_analysis_to_source

# 각 케이스: (label, url, heuristic_config)
# heuristic_config 는 heuristic 이 실제로 내놓을 법한 모양 — 불완전하거나 틀린 값 포함
cases = [
    (
        "hanin (표준 gnuboard — 회귀 체크)",
        "http://www.hanin.or.kr/bbs/board.php?bo_table=Information",
        {"base_url": "http://www.hanin.or.kr", "bbs_url": "/bbs/board.php",
         "board_table": "Information", "theme": "nariya",
         "selectors": {"list_rows": "ul.na-table > li"}},
    ),
    (
        "siemreap (page 쿼리 포함)",
        "https://siemreap.korean.net/bbs/board.php?bo_table=tb33&page=2",
        {"base_url": "https://siemreap.korean.net", "board_table": "tb33",
         "theme": "fz", "selectors": {}},
    ),
    (
        "ppomppu (비표준 zboard 경로 — 핵심 회귀방지)",
        "https://www.ppomppu.co.kr/zboard/zboard.php?id=guin",
        {"base_url": "https://www.ppomppu.co.kr", "board_table": "guin",
         "theme": "custom", "selectors": {}},
    ),
    (
        "radiokorea (/community/jobs.php — heuristic 이 base/board 공란)",
        "https://www.radiokorea.com/community/jobs.php",
        {"base_url": "", "board_table": "", "theme": "unknown", "selectors": {}},
    ),
]

for label, url, conf in cases:
    result = AnalysisResult(
        url=url, site_type=SiteType.GNUBOARD, confidence=0.7,
        config=conf, strategy_name="heuristic",
    )
    out = _gnuboard_analysis_to_source(result, url)
    print(f"=== {label} ===")
    print(f"  url        : {url}")
    print(f"  list_url   : {out['list_url']}")
    print(f"  list_params: {out['list_params']}")
    print(f"  base_url   : {out['base_url']}")
    print()
