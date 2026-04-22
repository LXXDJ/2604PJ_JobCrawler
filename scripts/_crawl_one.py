"""특정 site_id 하나만 크롤링 (새 페이지네이션 로직 검증용)."""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawlers"))

import dispatcher

target = sys.argv[1] if len(sys.argv) > 1 else "albamon"
sites = json.load(open(ROOT / "data" / "sites.json", encoding="utf-8"))
entry = next((s for s in sites if s.get("site_id") == target), None)
if not entry:
    print(f"site_id={target} 없음")
    sys.exit(1)

http_config = {"timeout": 30, "max_retries": 3, "retry_backoff": 2.0}
dispatcher.dispatch(entry, db_path=str(ROOT / "data" / "jobs.db"), http_config=http_config)
