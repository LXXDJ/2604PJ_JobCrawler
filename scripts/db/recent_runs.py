"""최근 crawl_runs 보기 (KST)."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.infra.db import get_conn

with get_conn() as c:
    rows = c.execute(
        "SELECT id, site_id, kind, "
        "datetime(started_at, '+9 hours') AS started_kst, "
        "datetime(ended_at,   '+9 hours') AS ended_kst, "
        "result, jobs_added, rows_seen "
        "FROM crawl_runs ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"  run#{r['id']:3} {r['site_id']:12} {r['kind']:6} "
              f"{r['started_kst']} → {r['ended_kst'] or '진행중'}  "
              f"{r['result'] or '-':8} added={r['jobs_added']} rows={r['rows_seen']}")
