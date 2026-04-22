import json
import sqlite3
import sys

sys.stdout.reconfigure(encoding='utf-8')

sites = json.load(open('data/sites.json', encoding='utf-8'))
before = len(sites)
sites = [s for s in sites if s.get('site_id') != 'kead']
json.dump(sites, open('data/sites.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'sites.json: {before} → {len(sites)}')

cands = json.load(open('data/site_candidates.json', encoding='utf-8'))
for c in cands:
    if c.get('name') == '장애인고용공단':
        c['skip'] = True
        c['skip_reason'] = 'DOM 크롤러가 메뉴 링크(공지사항/채용정보/훈련정보)만 수집 — 실제 공고 리스트 URL 아님'
        print(f'candidates skip: {c["name"]}')
json.dump(cands, open('data/site_candidates.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)

conn = sqlite3.connect('data/jobs.db')
removed = conn.execute("DELETE FROM jobs WHERE source='kead'").rowcount
conn.commit()
print(f'jobs.db kead rows deleted: {removed}')
