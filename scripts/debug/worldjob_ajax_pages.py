"""worldjob AJAX endpoint 가 query/POST 로 페이지네이션 되는지 직접 시험."""
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.fetchers.static import fetch as fetch_static


URLS = [
    "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do?currentPage=1",
    "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do?currentPage=2",
    "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do?currentPage=12",
    "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do?page=2",
    "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do?pageIndex=2",
]
PAT = re.compile(r"goView1\('([^']+)'")

for u in URLS:
    r = fetch_static(u, headers={
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.worldjob.or.kr/advnc/epmtList.do",
    })
    ids = list(dict.fromkeys(PAT.findall(r.text or "")))
    print(f"\n  {u}")
    print(f"    ok={r.ok} len={len(r.text)} rows={len(ids)}")
    print(f"    first: {ids[:3]}, last: {ids[-3:]}")
