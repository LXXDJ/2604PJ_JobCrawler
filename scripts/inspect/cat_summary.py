import json
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

data = json.load(open('data/site_candidates.json', encoding='utf-8'))
sites_data = json.load(open('data/sites.json', encoding='utf-8'))
registered_urls = {s.get('url', '').rstrip('/') for s in sites_data}

by_cat = defaultdict(lambda: {'total': 0, 'reg': 0, 'skip': 0, 'left': []})
for s in data:
    c = s.get('category', '?')
    by_cat[c]['total'] += 1
    url = s.get('url', '').rstrip('/')
    is_reg = s.get('registered') or any(url in r or r.rstrip('/') == url for r in registered_urls)
    if is_reg:
        by_cat[c]['reg'] += 1
    elif s.get('skip'):
        by_cat[c]['skip'] += 1
    else:
        by_cat[c]['left'].append((s.get('name', '?'), s.get('url', '')))

for k, v in sorted(by_cat.items()):
    print(f'{k}: total={v["total"]} reg={v["reg"]} skip={v["skip"]} left={len(v["left"])}')
    for name, url in v['left'][:15]:
        print(f'   - {name}  {url}')
