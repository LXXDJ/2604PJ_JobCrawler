import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

target = sys.argv[1]
conn = sqlite3.connect('data/jobs.db')
n = conn.execute("DELETE FROM jobs WHERE source=?", (target,)).rowcount
conn.commit()
print(f'jobs.db {target}: {n} rows deleted')
