"""incruit 페이지네이션 링크 패턴 확인."""
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, "crawlers")

from http_client import fetch
from bs4 import BeautifulSoup
from urllib.parse import parse_qs, urlparse

url = "https://job.incruit.com/jobdb_list/searchjob.asp"
html = fetch(url, timeout=30)

# 페이지네이션 영역 anchor 에서 page 관련 param 모으기
hrefs = re.findall(r'href="([^"]+)"', html)
# 숫자 파라미터만 있는 짧은 URL 선별
param_hits = {}
for h in hrefs:
    try:
        qs = parse_qs(urlparse(h).query)
    except Exception:
        continue
    for k, vs in qs.items():
        for v in vs:
            if v.isdigit() and 1 <= int(v) <= 200:
                param_hits.setdefault(k, set()).add(v)

print("페이지 후보 param (숫자 1~200 값):")
for k, vs in sorted(param_hits.items(), key=lambda x: -len(x[1])):
    if len(vs) < 3: continue
    print(f"  {k:20} distinct={len(vs):3}  sample={sorted(vs, key=int)[:6]}")

print("\nonclick 기반 페이지 이동 검색:")
for m in re.finditer(r'onclick="([^"]*?(page|Page|PG)[^"]*?)"', html):
    line = m.group(1)[:120]
    print(f"  {line}")
    if m.start() > 200000: break  # 처음 쪽만
