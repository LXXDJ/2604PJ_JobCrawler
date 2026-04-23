"""finished_at 이 없는 crawl_runs 를 'manual cleanup' 으로 마감."""
import sys, io, sqlite3, datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

con = sqlite3.connect("data/jobs.db")
rows = con.execute(
    "SELECT id, source FROM crawl_runs WHERE finished_at IS NULL"
).fetchall()
print("미완료 run:", rows)
now = datetime.datetime.now().isoformat()
for rid, sid in rows:
    con.execute(
        "UPDATE crawl_runs SET finished_at=?, error=? WHERE id=?",
        (now, "manual cleanup", rid),
    )
con.commit()
print("마크 완료")
