import json
import sys
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

data = json.load(open('data/site_candidates.json', encoding='utf-8'))

by_reason = defaultdict(list)
for s in data:
    if s.get('registered'):
        continue
    if not s.get('skip'):
        continue
    reason = s.get('skip_reason', '(사유 없음)')
    by_reason[reason].append((s.get('category', '?'), s.get('name', '?'), s.get('url', '')))

for reason, items in sorted(by_reason.items(), key=lambda x: -len(x[1])):
    print(f'\n[{len(items)}] {reason}')
    for cat, name, url in items:
        print(f'   · {cat} · {name}  {url}')

# 남은 (미등록 + 비skip)
left = [s for s in data if not s.get('registered') and not s.get('skip')]
print(f'\n\n=== 아직 시도 안한 / 결과 반영 전: {len(left)} ===')
for s in left:
    print(f'   · {s.get("category")} · {s.get("name")}  {s.get("url")}')
