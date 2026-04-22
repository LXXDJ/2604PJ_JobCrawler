import json
import sys
from collections import Counter, defaultdict

sys.stdout.reconfigure(encoding='utf-8')

cands = json.load(open('data/site_candidates.json', encoding='utf-8'))
sites = json.load(open('data/sites.json', encoding='utf-8'))
registered_urls = {s.get('url', '').rstrip('/') for s in sites}

def is_reg(url):
    u = url.rstrip('/')
    return any(u in r or r.rstrip('/') == u for r in registered_urls)

reasons = []
for c in cands:
    url = c.get('url', '')
    if is_reg(url):
        reasons.append(('등록됨', c))
    elif c.get('skip'):
        sr = c.get('skip_reason', '(사유없음)')
        if '수집 대상 아님' in sr or '대기업 정규직' in sr:
            reasons.append(('의도적 제외 — 대기업', c))
        elif 'DNS' in sr or 'domain expired' in sr:
            reasons.append(('도메인 죽음', c))
        elif 'Cloudflare' in sr or 'IP/ASN' in sr:
            reasons.append(('IP 차단 (유료 proxy 필수)', c))
        elif 'OpenAPI' in sr:
            reasons.append(('OpenAPI 채널 (스크래핑 비권장)', c))
        elif 'TOS' in sr or '로그인' in sr:
            reasons.append(('TOS/로그인 금지', c))
        elif '구인 아님' in sr or '공고 아님' in sr or '아님' in sr:
            reasons.append(('URL 잘못 — 내용 불일치', c))
        elif 'validator reject' in sr:
            reasons.append(('heuristic/validator 거부', c))
        elif 'selector' in sr:
            reasons.append(('selector 무효 (add 통과/크롤 0건)', c))
        else:
            reasons.append((f'기타 skip: {sr[:40]}', c))
    else:
        reasons.append(('아직 시도 안함', c))

counts = Counter(r[0] for r in reasons)
print(f'총 후보: {len(cands)}\n')
for k, n in counts.most_common():
    print(f'  {n:3d} · {k}')

print(f'\n합계: {sum(counts.values())}')
