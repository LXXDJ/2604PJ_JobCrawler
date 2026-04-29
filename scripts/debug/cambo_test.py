"""dynamic fetch 가 cambojob page 2 받는지 단독 시험."""
import io
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from crawlers.fetchers.dynamic import fetch as fd

URL = "https://www.cambojob.com/jobs/jobs_list/page/2.htm"
r = fd(URL)
ids = set(re.findall(r"jobs-show-(\d+)", r.text or ""))
print(f"dynamic {URL}: ok={r.ok} status={r.status} unique IDs={len(ids)}")
print(f"first 5 IDs: {list(ids)[:5]}")
