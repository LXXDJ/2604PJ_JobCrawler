import json
import sys

sys.stdout.reconfigure(encoding='utf-8')
target = sys.argv[1]

sites = json.load(open('data/sites.json', encoding='utf-8'))
before = len(sites)
sites = [s for s in sites if s.get('site_id') != target]
json.dump(sites, open('data/sites.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'sites.json: {before} → {len(sites)}')
