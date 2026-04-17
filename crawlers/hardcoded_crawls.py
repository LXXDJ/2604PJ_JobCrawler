"""
하드코딩된 기본 크롤러 등록 목록.

analyzer(`add` 명령)로 자동 등록이 불가능한 "특수 사이트" 만 여기에 둔다.
자동 등록이 가능한 일반 사이트는 data/sites.json 에 모인다.

왜 별도 파일?
  - main.py 의 SETTINGS 섹션을 얇게 유지 — 사이트 추가/제거 시 동작 설정을 건드릴 필요 없음.
  - "수동 유지가 필요한 특수 사이트" 가 이 파일 하나로 드러남.

제거 조건:
  - Playwright 기반 API 자동 발견이 구현되면 camhr 는 sites.json 쪽으로 이관 가능 → 이 파일은 비게 됨.
"""

# CamHR 는 Nuxt 기반 SPA — HTML 이 빈 껍데기라 heuristic 이 셀렉터를 못 뽑음.
# 현재는 수동 작성한 camhr_crawler.py 로만 수집 가능.
# (Playwright 기반 API 자동 발견이 붙으면 자동 등록으로 전환 예정)
CAMHR_MAX_PAGES = 3


REGISTERED_CRAWLS = [
    {
        "site_id": "camhr",
        "crawler": "camhr_crawler",
        "config": {"max_pages": CAMHR_MAX_PAGES},
    },
]
