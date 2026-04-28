"""DB 스키마 및 crawl_runs 상태 조사 (검증 1차용)."""
import sqlite3
from pathlib import Path

db = sqlite3.connect(Path(__file__).resolve().parent.parent.parent / "data" / "jobs.db")
cur = db.cursor()

tables = [r[0] for r in cur.execute("SELECT name FROM sqlite_master WHERE type='table'")]
print("TABLES:", tables)

for t in tables:
    cols = [c[1] for c in cur.execute(f"PRAGMA table_info({t})").fetchall()]
    cnt = cur.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    print(f"  {t}: rows={cnt} cols={cols}")

if "crawl_runs" in tables:
    print("\ncrawl_runs recent 10:")
    for r in cur.execute("SELECT * FROM crawl_runs ORDER BY id DESC LIMIT 10"):
        print(" ", r)
