"""사이트 names 를 utf-8 file 로 덤프 (콘솔 인코딩 회피)."""
import io
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.infra.db import get_conn

out = Path("data/site_names.txt")
with get_conn() as c:
    rows = list(c.execute("SELECT id, home_url, name FROM sites ORDER BY id"))

with open(out, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(f"{r['id']:15} | {r['name'] or '(none)':40} | {r['home_url']}\n")

print(f"wrote {len(rows)} rows to {out}")
