"""dispatcher._normalize_sources 역호환 확인 — v1/v2 엔트리 모두 sources 배열 반환."""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawlers"))

from dispatcher import _normalize_sources

sites = json.load(open(ROOT / 'data' / 'sites.json', encoding='utf-8'))
print(f'sites.json: {len(sites)} 개\n')

for s in sites[:5]:
    srcs = _normalize_sources(s)
    print(f'[{s["site_id"]}] extraction_method={s.get("extraction_method")} → sources={len(srcs)}개')
    for i, sub in enumerate(srcs):
        method = sub.get('extraction_method')
        endpoint = (sub.get('source') or {}).get('api_endpoint') or (sub.get('source') or {}).get('list_url') or '?'
        menu = sub.get('menu_name', '?')
        print(f'   {i+1}. menu={menu!r:12s} method={method:4s}  {endpoint[:80]}')

# v2 스타일 합성 확인
print('\n--- v2 스타일 테스트 ---')
v2_entry = {
    "site_id": "test",
    "sources": [
        {"menu_name": "전체", "extraction_method": "api", "source": {"api_endpoint": "https://x/a"}, "pagination": {}},
        {"menu_name": "정직원", "extraction_method": "api", "source": {"api_endpoint": "https://x/b"}, "pagination": {}},
    ]
}
srcs = _normalize_sources(v2_entry)
print(f'v2 entry sources 추출: {len(srcs)}개')
for i, sub in enumerate(srcs):
    print(f'   {i+1}. {sub}')
