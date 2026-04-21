"""incruit 페이지네이션 영역 DOM 확인."""
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, "crawlers")

from http_client import fetch
from bs4 import BeautifulSoup

url = "https://job.incruit.com/jobdb_list/searchjob.asp"
html = fetch(url, timeout=30)
soup = BeautifulSoup(html, "lxml")

# "2", "3", "다음" 같은 페이지 버튼 찾기
# 흔한 위치: .paging, .pagination, .pg, .n_paging 등
for cand in [".n_paging", ".paging", ".pagination", ".pg_wrap", "#pgWrap",
             ".pagenation", "div[class*='page']", "ul.pg"]:
    n = soup.select(cand)
    if n:
        print(f"=== {cand} ({len(n)}건) ===")
        print(str(n[0])[:500])
        print()

# 마지막 방법: 텍스트가 "2"이고 href 있는 anchor
print("\n페이지 숫자 anchor 후보 (텍스트 == 1~10):")
for a in soup.select("a"):
    t = a.get_text(strip=True)
    if t.isdigit() and 1 <= int(t) <= 50 and a.get("href"):
        print(f"  text={t!r:4}  href={a['href'][:140]}")
