import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

conn = sqlite3.connect('data/jobs.db')
rows = conn.execute('''
    SELECT source, COUNT(*) as n,
           MIN(first_seen_at) as first,
           MAX(last_seen_at) as last
    FROM jobs
    GROUP BY source
    ORDER BY n DESC
''').fetchall()

total = 0
print(f'{"source":20s}  {"count":>7s}  {"first_seen":19s}  {"last_seen":19s}')
print('-' * 72)
for source, n, first, last in rows:
    print(f'  {source:20s}  {n:>7,d}  {(first or "")[:19]}  {(last or "")[:19]}')
    total += n
print('-' * 72)
print(f'  {"total":20s}  {total:>7,d}')
