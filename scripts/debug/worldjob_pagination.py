"""worldjob page 1 vs page 50 의 _jsid 비교 — pagination 정상 작동 여부 진단."""
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.fetchers.dynamic import fetch as fetch_dynamic


URLS = [
    "https://www.worldjob.or.kr/advnc/epmtList.do?menuId=1000002033&currentPage=1",
    "https://www.worldjob.or.kr/advnc/epmtList.do?menuId=1000002033&currentPage=50",
    "https://www.worldjob.or.kr/advnc/epmtList.do?menuId=1000002033&page=1",
    "https://www.worldjob.or.kr/advnc/epmtList.do?menuId=1000002033&page=50",
]

PAT = re.compile(r"goView1\('([^']+)'")


def main():
    for u in URLS:
        r = fetch_dynamic(u)
        ids = list(dict.fromkeys(PAT.findall(r.text or "")))
        print(f"\n  {u}")
        print(f"    rows={len(ids)}, first 5: {ids[:5]}, last 5: {ids[-5:]}")


if __name__ == "__main__":
    main()
