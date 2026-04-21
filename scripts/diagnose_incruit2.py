"""incruit 리스트 URL 에 page=1 붙이면 다른 응답이 오는지 확인."""
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, "crawlers")

from http_client import fetch
from bs4 import BeautifulSoup

url = "https://job.incruit.com/jobdb_list/searchjob.asp"

for label, params in [
    ("no params", None),
    ("page=1", {"page": 1}),
    ("today=y", {"today": "y"}),
    ("today=y&page=1", {"today": "y", "page": 1}),
    ("today=y&page=2", {"today": "y", "page": 2}),
]:
    html = fetch(url, params=params, timeout=30)
    soup = BeautifulSoup(html, "lxml")
    rows = soup.select("ul.c_row")
    print(f"{label:20} → len={len(html):6}  rows={len(rows)}")
