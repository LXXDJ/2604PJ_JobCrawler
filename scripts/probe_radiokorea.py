"""radiokorea jobs.php 의 리스트 아이템 HTML 구조 확인."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
sys.path.insert(0, "crawlers")
from http_client import fetch
from bs4 import BeautifulSoup

html = fetch("https://www.radiokorea.com/community/jobs.php", timeout=30)
soup = BeautifulSoup(html, "lxml")

rows = soup.select(".pp-list > li")
print(f"총 {len(rows)} 행")
if rows:
    # 첫 3개 행의 첫 100자 HTML
    for i, r in enumerate(rows[:3]):
        print(f"--- row {i} ---")
        print(str(r)[:600])
        print()
