import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

UPDATES = {
    "새일센터": "로그인 필수 (2026-04-21 사용자 확인)",
    "탤런트뱅크": "제외 대상 (2026-04-21 사용자 확인)",
    "SK": "SPA 동적 렌더링 — /Recruit 도 Playwright 로 API endpoint 발견 못 함, 현재 add 파이프라인으로 불가",
    "인천일자리포털": None,  # 사용자 URL 로 재등록 성공 → skip 해제
    "이랜서": None,  # 재등록 성공 → skip 해제
}

data = json.load(open('data/site_candidates.json', encoding='utf-8'))
for c in data:
    name = c.get('name')
    if name in UPDATES:
        reason = UPDATES[name]
        if reason is None:
            c.pop('skip', None)
            c.pop('skip_reason', None)
            c['registered'] = True
            print(f'registered: {name}')
        else:
            c['skip'] = True
            c['skip_reason'] = reason
            print(f'skip update: {name} — {reason[:60]}')

# URL 교체 (candidates 파일에 반영)
for c in data:
    if c.get('name') == '인천일자리포털':
        c['url'] = 'https://www.incheon.go.kr/job/index'
    elif c.get('name') == '이랜서':
        c['url'] = 'https://www.elancer.co.kr/list-partner'
    elif c.get('name') == 'SK':
        c['url'] = 'https://www.skcareers.com/Recruit'

json.dump(data, open('data/site_candidates.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
