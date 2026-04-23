import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
c = sqlite3.connect('data/jobs.db')
cols = [r[1] for r in c.execute('PRAGMA table_info(jobs)').fetchall()]
print(f'columns: {cols}')
for site in ('kead', 'kt', 'seoul', 'freemoa', 'seoulkcr', 'lg'):
    rows = c.execute('SELECT title, link FROM jobs WHERE source=? LIMIT 5', (site,)).fetchall()
    print(f'\n=== {site} ({len(rows)} shown) ===')
    for t, u in rows:
        print(f'  {t[:70]:70s}  {(u or "")[:70]}')
