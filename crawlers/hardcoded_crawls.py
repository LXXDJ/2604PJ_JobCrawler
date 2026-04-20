"""
하드코딩된 기본 크롤러 등록 목록.

analyzer(`add` 명령)로 자동 등록이 불가능한 "특수 사이트" 만 여기에 둔다.
자동 등록이 가능한 일반 사이트는 data/sites.json 에 모인다.

왜 별도 파일?
  - main.py 의 SETTINGS 섹션을 얇게 유지 — 사이트 추가/제거 시 동작 설정을 건드릴 필요 없음.
  - "수동 유지가 필요한 특수 사이트" 가 이 파일 하나로 드러남.

현재 상태 (Phase 3):
  - CamHR 은 PlaywrightDiscoveryStrategy + api_crawler 로 이주 완료 →
    data/sites.json 쪽으로 이동.
  - 이 리스트는 지금 비어 있지만, 자동 발견/DOM/embedded 어느 경로로도 다룰 수 없는
    특수 케이스가 나타날 때 (예: 세션 필수 + 인증 필요한 API) 임시로 사용.
"""

REGISTERED_CRAWLS: list = []
