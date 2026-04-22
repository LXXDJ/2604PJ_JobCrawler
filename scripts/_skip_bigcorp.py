import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

REASON = "수집 대상 아님 (2026-04-21 사용자 결정: 대기업 정규직 채용은 서비스 타겟과 맞지 않음)"

# 이미 등록된 대기업 몇 개는 유지. 그 외 대기업_채용 카테고리의 미등록은 전부 skip.
KEEP_REGISTERED = True  # registered=True 는 건드리지 않음

data = json.load(open('data/site_candidates.json', encoding='utf-8'))
changed = 0
for s in data:
    if s.get('category') != '대기업_채용':
        continue
    if s.get('registered'):
        continue
    if s.get('skip'):
        continue
    s['skip'] = True
    s['skip_reason'] = REASON
    changed += 1
    print(f'skip: {s.get("name")}  {s.get("url")}')

json.dump(data, open('data/site_candidates.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'\n총 skip 마킹: {changed}')
