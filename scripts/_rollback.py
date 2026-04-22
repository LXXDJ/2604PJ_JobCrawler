import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

TARGET = 'heykorean'
sites = json.load(open('data/sites.json', encoding='utf-8'))
before = len(sites)
sites = [s for s in sites if s.get('site_id') != TARGET]
json.dump(sites, open('data/sites.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'sites.json: {before} → {len(sites)}')

conn = sqlite3.connect('data/jobs.db')
n = conn.execute(f"DELETE FROM jobs WHERE source=?", (TARGET,)).rowcount
conn.commit()
print(f'jobs.db {TARGET} rows deleted: {n}')
