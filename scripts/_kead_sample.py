import sqlite3
import sys
sys.stdout.reconfigure(encoding='utf-8')
conn = sqlite3.connect('data/jobs.db')
for site in ('kead', 'kt', 'seoul', 'freemoa', 'seoulkcr'):
    rows = conn.execute(f'SELECT title, url FROM jobs WHERE source=? LIMIT 5', (site,)).fetchall()
    print(f'\n=== {site} ({len(rows)} rows shown) ===')
    for t, u in rows:
        print(f'  {t[:70]:70s}  {u[:70]}')
