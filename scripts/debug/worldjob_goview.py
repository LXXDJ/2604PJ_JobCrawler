"""worldjob 의 goView1 JS 함수 정의를 찾아서 detail URL 패턴 확인."""
import io
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from crawlers.fetchers.static import fetch as fetch_static


# epmtList.do 페이지 HTML 에 goView1 정의 있는지
URLS = [
    "https://www.worldjob.or.kr/advnc/epmtList.do?menuId=1000002033",
]

for url in URLS:
    print(f"\n=== {url}")
    r = fetch_static(url)
    html = r.text or ""
    # function goView1, var goView1, goView1 = function 등 다양한 정의 패턴
    patterns = [
        r"function\s+goView1\s*\([^)]*\)\s*\{[^}]{0,800}",
        r"goView1\s*=\s*function\s*\([^)]*\)\s*\{[^}]{0,800}",
        r"var\s+goView1\s*=[^;]{0,500}",
    ]
    for pat in patterns:
        for m in re.finditer(pat, html):
            print(f"\n--- match (len={len(m.group(0))})")
            print(m.group(0)[:600])

    # script 태그 src 들도 출력 — 외부 js 에 있을 수 있음
    print("\n--- external scripts:")
    for m in re.finditer(r'<script[^>]+src="([^"]+)"', html)[:0]:
        pass
    for m in re.finditer(r'<script[^>]+src="([^"]+)"', html):
        print(f"  {m.group(1)}")
