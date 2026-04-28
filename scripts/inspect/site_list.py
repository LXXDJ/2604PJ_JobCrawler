"""등록된 각 사이트의 site_id / DB count / 주 URL 을 보기 좋게 나열.

사용자가 직접 브라우저로 가서 '몇 건 있는지 vs DB 몇 건' 대조 검증하기 위한 목록.
"""
import io
import json
import sqlite3
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent.parent
db = sqlite3.connect(ROOT / "data" / "jobs.db")
cur = db.cursor()

counts = dict(cur.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source"))

with open(ROOT / "data" / "sites.json", encoding="utf-8") as f:
    sites = json.load(f)

print(f"{'#':>2} {'site_id':<16} {'DB':>6}  URL")
print("-" * 100)

rows = []
for e in sites:
    sid = e["site_id"]
    cnt = counts.get(sid, 0)
    # 주 URL: sources[0] list_url > root url
    url = ""
    srcs = e.get("sources") or []
    if srcs:
        url = (srcs[0].get("source") or {}).get("list_url", "")
    if not url:
        url = e.get("url") or e.get("root_url") or (e.get("source") or {}).get("list_url", "")
    rows.append((sid, cnt, url, len(srcs)))

rows.sort(key=lambda r: -r[1])
for i, (sid, cnt, url, nsrc) in enumerate(rows, 1):
    tag = f" (sources={nsrc})" if nsrc > 1 else ""
    print(f"{i:>2} {sid:<16} {cnt:>6}  {url}{tag}")
