"""
사이트별 크롤링 설정 파일

새로운 그누보드 사이트를 추가하려면:
1. 이 파일에 설정 dict를 추가
2. SITES 리스트에 등록
그러면 크롤러가 자동으로 해당 사이트도 수집한다.

각 사이트마다 테마가 달라서 CSS 셀렉터가 다르다.
브라우저 개발자도구(F12)로 HTML 구조를 분석해서 셀렉터를 찾으면 된다.
"""

# =============================================================
# 사이트 설정 구조 설명
# =============================================================
# name         : 사이트 식별 이름 (파일명에도 사용)
# base_url     : 사이트 루트 URL
# board_url    : 게시판 목록 URL
# board_table  : 그누보드 게시판 식별자 (bo_table 파라미터)
# verify_ssl   : SSL 인증서 검증 여부
#
# selectors    : CSS 셀렉터 모음 (사이트 테마마다 다름)
#   list_rows       : 게시글 행 목록
#   subject_link    : 제목 + 링크가 있는 a 태그
#   author          : 작성자
#   date            : 등록일
#   hit             : 조회수
#   content         : 상세 페이지 본문
#   total_info      : 전체 건수/페이지 정보 영역
#
# parse_mode   : 날짜/조회수 추출 방식
#   "sr_only"  → sr-only 라벨 기반 (한인회 나리야 테마)
#   "direct"   → 클래스로 직접 접근 (시엠립 fz 테마)


HANIN = {
    "name": "hanin",
    "description": "재캄보디아한인회 구인구직",
    "base_url": "http://www.hanin.or.kr",
    "board_url": "http://www.hanin.or.kr/bbs/board.php",
    "board_table": "Information",
    "verify_ssl": False,  # self-signed 인증서

    "selectors": {
        "list_rows": "ul.na-table > li",
        "subject_link": "a.na-subject",
        "author": "span.sv_member",
        "date": "div.d-md-table-cell",       # sr-only 방식으로 내부 탐색
        "hit": "div.d-md-table-cell",         # sr-only 방식으로 내부 탐색
        "content": "div.view-content",
        "total_info": "#bo_list_total",
    },

    "parse_mode": "sr_only",
}

SIEMREAP = {
    "name": "siemreap",
    "description": "재캄보디아시엠립한인회 구인구직",
    "base_url": "https://siemreap.korean.net",
    "board_url": "https://siemreap.korean.net/bbs/board.php",
    "board_table": "tb33",
    "verify_ssl": True,

    "selectors": {
        "list_rows": "ul.fz_list > li",
        "subject_link": "div.fz_subject > a",
        "author": "span.sv_member",
        "date": "div.fz_date",
        "hit": "div.fz_hit",
        "content": "#bo_v_con",
        "total_info": "div.fz_total_count",
    },

    "parse_mode": "direct",
}

# 크롤링할 사이트 목록 — 여기에 추가하면 자동으로 수집됨
SITES = [HANIN, SIEMREAP]
