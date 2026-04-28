"""worldjob AJAX 응답의 paging block HTML 내용 dump."""
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.fetchers.static import fetch as fetch_static


r = fetch_static(
    "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do",
    headers={
        "X-Requested-With": "XMLHttpRequest",
        "Referer": "https://www.worldjob.or.kr/advnc/epmtList.do",
    },
)
m = re.search(r"bbs_text_type_paging.{0,2000}", r.text or "", re.S)
print(m.group(0)[:1800] if m else "(no paging block)")
