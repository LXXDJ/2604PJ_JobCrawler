import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

data = json.load(open('data/site_candidates.json', encoding='utf-8'))

print("=" * 80)
print("URL 잘못 (내용 불일치) — 올바른 구인 URL 찾으면 해결 가능")
print("=" * 80)
for c in data:
    if not c.get('skip'):
        continue
    sr = c.get('skip_reason', '')
    if '공고 아님' in sr or '구인 아님' in sr or '아닌' in sr or '불일치' in sr or '우수사례' in sr:
        print(f'\n· {c.get("category")} / {c.get("name")}')
        print(f'  기존 URL: {c.get("url")}')
        print(f'  문제    : {sr}')

print("\n\n" + "=" * 80)
print("도메인 죽음 (공용 DNS 재확인됨)")
print("=" * 80)
for c in data:
    if not c.get('skip'):
        continue
    sr = c.get('skip_reason', '')
    if 'DNS' in sr or 'domain expired' in sr or 'NXDOMAIN' in sr:
        print(f'  · {c.get("category")} / {c.get("name"):15s}  {c.get("url")}')
