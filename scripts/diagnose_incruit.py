"""incruit 0건 원인 진단 — 현재 selectors 가 HTML 에 매치되는지."""
import sys, io, json
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, "crawlers")

from http_client import fetch
from bs4 import BeautifulSoup

with open("data/sites.json", encoding="utf-8") as f:
    entry = next(e for e in json.load(f) if e["site_id"] == "incruit")

src = entry["source"]
print(f"url        : {entry['url']}")
print(f"list_url   : {src['list_url']}")
print(f"list_rows  : {src['selectors']['list_rows']}")
print(f"subject_link: {src['selectors']['subject_link']}")
print()

html = fetch(src["list_url"], timeout=30)
print(f"HTML len   : {len(html)}")

soup = BeautifulSoup(html, "lxml")
rows = soup.select(src["selectors"]["list_rows"])
print(f"list_rows 매치 수: {len(rows)}")
if rows:
    # 첫 행에서 subject_link 매치
    first = rows[0]
    link = first.select_one(src["selectors"]["subject_link"])
    print(f"첫 행 subject_link: {link!r}")
else:
    # list_rows 가 안 잡히면 c_row 변종이나 다른 후보 검색
    print("\n현재 selector 0개 매치. 후보 탐색:")
    for cand in ["ul.c_row", "li.c_row", ".c_row", ".list-item", ".jobItem",
                 "ul.hotListBody", ".job_item", "tr.type1", "div.list_wrap > li",
                 "ul > li", ".n_job_list_default"]:
        n = len(soup.select(cand))
        if n >= 5:
            print(f"  {cand:30} → {n}건")
    # 타이틀처럼 보이는 영역
    print("\nanchor 중 '채용' 관련 텍스트 상위 5개:")
    import re
    for a in soup.select("a")[:500]:
        t = a.get_text(strip=True)
        if t and re.search(r"채용|모집|구인", t) and len(t) < 80:
            print(f"  {t}  ← href={a.get('href','')[:60]}")
