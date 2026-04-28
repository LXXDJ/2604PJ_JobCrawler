"""worldjob page 11 의 누락 1건 추적."""
import io
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from bs4 import BeautifulSoup
from crawlers.fetchers.static import fetch as fetch_static
from crawlers.extractors.list_extractor import extract_list_multi


URL = "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do?pageIndex=11"
HEADERS = {"X-Requested-With": "XMLHttpRequest",
           "Referer": "https://www.worldjob.or.kr/advnc/epmtList.do"}

r = fetch_static(URL, headers=HEADERS)
s = BeautifulSoup(r.text, "html.parser")
boxes = s.select("div.post-box")

# 각 box 의 goView1 ID 와 우리 extractor 결과
multi = extract_list_multi(r.text, URL)
post_box_ext = next(e for e in multi.candidates if "post-box" in e.container_signature)
ext_urls = [row.detail_url for row in post_box_ext.rows]
ext_ids = {re.search(r"_jsid=([^&]+)", u).group(1) for u in ext_urls if "_jsid=" in u}

# raw IDs per box
raw_per_box = []
for i, box in enumerate(boxes):
    ids = []
    for a in box.find_all("a", href=True):
        m = re.search(r"goView1\('([^']+)'", a["href"])
        if m:
            ids.append(m.group(1))
            break
    raw_per_box.append((i, ids[0] if ids else None, box))

raw_ids_set = {x[1] for x in raw_per_box if x[1]}
missing = raw_ids_set - ext_ids
print(f"raw_unique={len(raw_ids_set)}, extracted={len(ext_ids)}, missing={missing}")

for i, rid, box in raw_per_box:
    if rid in missing:
        print(f"\n--- box #{i} (raw ID {rid}) — 우리 추출 누락")
        print(f"  text: {box.get_text(' ', strip=True)[:200]}")
        for a in box.find_all("a", href=True):
            print(f"    href={a['href']}  text={a.get_text(' ', strip=True)!r}")
