import json
import sys

sys.stdout.reconfigure(encoding='utf-8')
d = json.load(open('data/site_candidates.json', encoding='utf-8'))
for c in d:
    if c.get('category') == '교민':
        print(f'{c["name"]:15s}  reg={c.get("registered")}  skip={c.get("skip")}  url={c.get("url")}')
