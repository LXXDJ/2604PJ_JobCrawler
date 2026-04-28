"""worldjob 적재 진행 체크 (배치 도는 중 호출)."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.infra.db import get_conn

with get_conn() as c:
    n = c.execute("SELECT COUNT(*) FROM jobs WHERE site_id=?", ("worldjob",)).fetchone()[0]
    last = c.execute(
        "SELECT title, last_seen_at FROM jobs WHERE site_id=? ORDER BY id DESC LIMIT 1",
        ("worldjob",),
    ).fetchone()
    print(f"worldjob jobs: {n}")
    if last:
        print(f"last inserted: {last['title'][:60]} @ {last['last_seen_at']}")
    runs = c.execute(
        "SELECT id, started_at, ended_at, result, jobs_added, rows_seen "
        "FROM crawl_runs WHERE site_id=? ORDER BY id DESC LIMIT 3",
        ("worldjob",),
    ).fetchall()
    print("recent runs:")
    for r in runs:
        print(f"  run#{r['id']}  start={r['started_at']}  end={r['ended_at']}  "
              f"result={r['result']}  added={r['jobs_added']}  rows={r['rows_seen']}")
