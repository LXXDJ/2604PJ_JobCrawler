"""auto menu_discovery 로 등록된 사이트 (sources[] 채워진) 목록."""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
ROOT = Path(__file__).resolve().parent.parent.parent

with open(ROOT / "data" / "sites.json", encoding="utf-8") as f:
    sites = json.load(f)

multi = [
    (e["site_id"], len(e.get("sources") or []), e.get("url"))
    for e in sites if e.get("sources")
]
multi.sort(key=lambda x: -x[1])

print(f"총 {len(multi)}개 사이트 (auto menu_discovery + LLM 등록)\n")
print(f"{'site_id':<16} {'sources':>7}  url")
print("-" * 90)
for sid, n, url in multi:
    print(f"  {sid:<14} {n:>7}  {url}")
