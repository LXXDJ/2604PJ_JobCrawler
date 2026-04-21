"""오늘 자정 이후 crawl_runs 결과 확인."""
import sqlite3, io, sys, datetime
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

since = sys.argv[1] if len(sys.argv) > 1 else datetime.date.today().isoformat()
con = sqlite3.connect("data/jobs.db")
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT source, started_at, new_count, updated_count, error "
    "FROM crawl_runs WHERE started_at >= ? ORDER BY started_at DESC",
    (since,),
).fetchall()
for r in rows:
    print(dict(r))
