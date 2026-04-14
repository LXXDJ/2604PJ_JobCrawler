# JobCrawler - 구인구직 사이트 크롤링 프로젝트

캄보디아 교민 대상 구인구직 사이트를 크롤링하며 웹 크롤링 기술을 단계적으로 학습하는 프로젝트.

## 프로젝트 목표

- 정적/동적 크롤링 기법 학습
- 봇 탐지 우회 방법 습득
- 매일 자동 증분 수집 파이프라인 구축

## 진행 단계

| 단계 | 대상 사이트 | 학습 내용 | 상태 |
|------|------------|-----------|------|
| Step 1 | 재캄보디아한인회 + 시엠립한인회 | 정적 크롤링, 설정 기반 멀티사이트 크롤러 | 완료 |
| Step 2 | CamHR | 봇 탐지 우회 (Scrapling/Playwright), DB 저장, 증분 수집 | 예정 |

## 크롤링 대상

### Step 1 - 캄보디아 한인 커뮤니티 (그누보드 기반)

| 사이트 | URL | 게시글 수 |
|--------|-----|-----------|
| 재캄보디아한인회 | http://www.hanin.or.kr/bbs/board.php?bo_table=Information | 14건 |
| 시엠립한인회 | https://siemreap.korean.net/bbs/board.php?bo_table=tb33 | 3건 |

두 사이트 모두 그누보드 기반이지만 테마(CSS 클래스)가 다르다. 사이트별 설정을 `config.py`에 분리하여 하나의 크롤러(`gnuboard_crawler.py`)로 여러 사이트를 수집한다.

### Step 2 - CamHR (예정)

- URL: https://www.camhr.com/
- 캄보디아 최대 구인구직 사이트

## 프로젝트 구조

```
2604PJ_JobCrawler/
├── crawlers/
│   ├── config.py              # 사이트별 크롤링 설정 (CSS 셀렉터 등)
│   ├── gnuboard_crawler.py    # 그누보드 범용 크롤러 (설정 기반)
│   └── hanin_crawler.py       # 한인회 단일 크롤러 (학습용 초기 버전)
├── data/
│   ├── hanin_jobs.json        # 한인회 수집 데이터
│   └── siemreap_jobs.json     # 시엠립한인회 수집 데이터
├── requirements.txt
├── .gitignore
└── README.md
```

## 설치 및 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# 전체 사이트 크롤링 (한인회 + 시엠립)
cd crawlers
python gnuboard_crawler.py
```

## 수집 데이터 형식

```json
{
  "wr_id": "58",
  "title": "2026학년도 프놈펜한국국제학교 중등 체육 시간 강사 모집(재공고)",
  "author": "튜더",
  "date": "2026.02.05",
  "hit": "83",
  "link": "http://www.hanin.or.kr/bbs/board.php?bo_table=Information&wr_id=58",
  "content": "프놈펜한국국제학교에서 아래와 같이 중등 체육 시간 강사를 모집하오니...",
  "source": "hanin",
  "crawled_at": "2026-04-14T17:05:08.522835"
}
```

## 새로운 그누보드 사이트 추가 방법

`crawlers/config.py`에 설정을 추가하면 된다:

```python
NEW_SITE = {
    "name": "new_site",
    "description": "새 사이트 설명",
    "base_url": "https://example.com",
    "board_url": "https://example.com/bbs/board.php",
    "board_table": "job",
    "verify_ssl": True,
    "selectors": {
        "list_rows": "ul.fz_list > li",       # F12로 확인
        "subject_link": "div.fz_subject > a",  # F12로 확인
        "author": "span.sv_member",
        "date": "div.fz_date",
        "hit": "div.fz_hit",
        "content": "#bo_v_con",
        "total_info": "div.fz_total_count",
    },
    "parse_mode": "direct",
}

SITES = [HANIN, SIEMREAP, NEW_SITE]  # 리스트에 추가
```

## 기술 스택

- **Python 3.11**
- **requests** - HTTP 요청
- **BeautifulSoup4 + lxml** - HTML 파싱
- (Step 2 예정) Scrapling, Playwright - 봇 탐지 우회 / 동적 페이지 처리
- (Step 2 예정) SQLite - 증분 수집용 DB
