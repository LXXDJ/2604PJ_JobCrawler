"""
site_id → 사용자 노출용 한글 라벨 매핑.

DB 의 source 컬럼은 영문 site_id 로 고정 (크롤러 내부 키). 사람이 보는 UI
(대시보드 / Slack 알림) 에서만 이 매핑으로 한글 변환한다.

새 사이트를 `python main.py add` 로 등록했다면 여기에도 한 줄 추가. 누락되면
`label()` 이 site_id 를 그대로 돌려주므로 동작엔 문제 없지만 가독성 떨어짐.
"""

SITE_LABELS = {
    "hanin": "재캄보디아한인회",
    "siemreap": "시엠립한인회",
    "camhr": "CamHR",
    "jobkorea": "잡코리아",
    "incruit": "인크루트",
    "wanted": "원티드",
    "ppomppu": "뽐뿌 구인정보",
    "alba": "알바천국",
    "radiokorea": "라디오코리아",
    "rocketpunch": "로켓펀치",
    "jumpit": "점핏",
    "saramin": "사람인",
    "jobplanet": "잡플래닛",
    "peoplenjob": "피플앤잡",
    "career": "커리어",
    "findall": "벼룩시장",
    "hibrain": "하이브레인",
    "albamon": "알바몬",
    "seoulkcr": "서울교차로",
    "samsungcareers": "삼성 채용",
    "kt": "KT 채용",
    "coupang": "쿠팡 채용",
    "kakao": "카카오 채용",
    "daangn": "당근 채용",
    "linecorp": "라인 채용",
    "seoul": "서울일자리포털",
    "kotra": "KOTRA",
    "gamejob": "게임잡",
    "mediajob": "미디어잡",
    "freemoa": "프리모아",
}


def label(site_id: str) -> str:
    """site_id → 한글 라벨. 매핑 없으면 원본 반환 (침묵 fallback)."""
    return SITE_LABELS.get(site_id, site_id)
