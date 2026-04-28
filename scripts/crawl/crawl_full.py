"""지정한 site_id 를 전체 페이지 범위로 크롤링 (pagination 설정 그대로 준수)."""
import sys, io, json, os
# line-buffered utf-8 → tee / 백그라운드 실행 시 로그 유실 방지
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", line_buffering=True)
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "crawlers"))

import dispatcher

DB_PATH = os.path.join(ROOT, "data", "jobs.db")
HTTP = {"timeout": 30, "max_retries": 3, "retry_backoff": 2.0}

targets = sys.argv[1:]
if not targets:
    print("Usage: python scripts/crawl_full.py <site_id> [<site_id>...]")
    sys.exit(1)

with open(os.path.join(ROOT, "data", "sites.json"), encoding="utf-8") as f:
    entries = json.load(f)

for tid in targets:
    entry = next((e for e in entries if e["site_id"] == tid), None)
    if entry is None:
        print(f"[SKIP] {tid} not found in sites.json")
        continue
    print(f"\n>>> {tid}")
    try:
        dispatcher.dispatch(entry, db_path=DB_PATH, http_config=HTTP)
    except Exception as e:
        print(f"  [ERROR] {tid} failed: {type(e).__name__}: {e}")
