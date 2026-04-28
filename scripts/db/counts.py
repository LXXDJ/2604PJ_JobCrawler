"""사이트별 job 누적 카운트."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.infra.db import get_conn

with get_conn() as c:
    rows = c.execute(
        "SELECT site_id, COUNT(*) AS n FROM jobs GROUP BY site_id ORDER BY site_id"
    ).fetchall()
    for r in rows:
        print(f"  {r['site_id']:20}  {r['n']}")
    total = c.execute("SELECT COUNT(*) FROM jobs").fetchone()[0]
    print(f"\n  total: {total}")
