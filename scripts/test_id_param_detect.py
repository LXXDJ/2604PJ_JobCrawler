"""상세링크 ID param 자동검출 — 실제 3개 사이트로 검증."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, "crawlers")

from http_client import fetch
from analyzer.strategies.heuristic import _detect_detail_id_param

cases = [
    ("hanin", "http://www.hanin.or.kr/bbs/board.php?bo_table=Information", "wr_id"),
    ("ppomppu", "https://www.ppomppu.co.kr/zboard/zboard.php?id=guin", "no"),
    ("radiokorea", "https://www.radiokorea.com/community/jobs.php", "id"),
    ("siemreap", "https://siemreap.korean.net/bbs/board.php?bo_table=tb33", "wr_id"),
]

for label, url, expected in cases:
    try:
        html = fetch(url, timeout=30)
    except Exception as e:
        print(f"{label:12} FAIL fetch: {e}")
        continue
    detected = _detect_detail_id_param(html)
    mark = "✓" if detected == expected else "✗"
    print(f"{mark} {label:12} detected={detected!r:10} expected={expected!r}")
