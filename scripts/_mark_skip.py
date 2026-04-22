import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

DEAD = {
    "https://www.contentjob.co.kr/": "DNS 죽음 (domain expired)",
    "https://www.teacherjob.co.kr/": "DNS 죽음",
    "https://www.medijob.co.kr/": "DNS 죽음",
    "https://www.overseajob.co.kr/": "DNS 죽음",
    "https://www.researchjob.co.kr/": "DNS 죽음",
    "https://www.productionjob.co.kr/": "DNS 죽음",
}
REJECTED = {
    "https://www.worker.co.kr/": "validator reject (건설워커, 72s 타임아웃 계열)",
    "https://www.foodjob.co.kr/": "validator reject",
    "https://www.logisticsjob.co.kr/": "validator reject",
    "https://www.hoteljob.co.kr/": "validator reject",
    "https://www.teacherville.co.kr/": "validator reject (강의 플랫폼, 구인 페이지 없음)",
}

data = json.load(open('data/site_candidates.json', encoding='utf-8'))
changed = 0
for s in data:
    url = s.get('url', '')
    if url in DEAD and not s.get('skip'):
        s['skip'] = True
        s['skip_reason'] = DEAD[url]
        changed += 1
    elif url in REJECTED and not s.get('skip'):
        s['skip'] = True
        s['skip_reason'] = REJECTED[url]
        changed += 1

json.dump(data, open('data/site_candidates.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print(f'skip 마킹: {changed}')
