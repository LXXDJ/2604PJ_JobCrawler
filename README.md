# JobCrawler - 구인구직 사이트 크롤링 프로젝트

캄보디아 교민 대상 구인구직 사이트를 크롤링하며 웹 크롤링 기술을 단계적으로 학습하는 프로젝트.

## 프로젝트 목표

- 정적/동적 크롤링 기법 학습
- 봇 탐지 우회 방법 습득
- 매일 자동 증분 수집 파이프라인 구축

## 진행 단계

| 단계 | 대상 사이트 | 학습 내용 | 상태 |
|------|------------|-----------|------|
| Step 1 | 재캄보디아한인회 + 시엠립한인회 | HTML 파싱 (requests + BeautifulSoup), 설정 기반 멀티사이트 | 완료 |
| Step 2 | CamHR | API 크롤링, SPA 사이트 대응, Playwright 네트워크 캡처 | 완료 |

## 크롤링 대상

### Step 1 - 캄보디아 한인 커뮤니티 (그누보드 기반, HTML 파싱)

| 사이트 | URL | 게시글 수 |
|--------|-----|-----------|
| 재캄보디아한인회 | http://www.hanin.or.kr/bbs/board.php?bo_table=Information | 14건 |
| 시엠립한인회 | https://siemreap.korean.net/bbs/board.php?bo_table=tb33 | 3건 |

두 사이트 모두 그누보드 기반이지만 테마(CSS 클래스)가 다르다. 사이트별 설정을 `config.py`에 분리하여 하나의 크롤러(`gnuboard_crawler.py`)로 여러 사이트를 수집한다.

### Step 2 - CamHR (API 크롤링)

- URL: https://www.camhr.com/
- 캄보디아 최대 구인구직 사이트 (1,800+ 공고)
- Nuxt.js(Vue SSR) 기반 SPA — HTML에 데이터 없음
- REST API를 직접 호출하여 수집

API를 찾는 과정:
1. Playwright로 브라우저를 띄워 네트워크 요청 캡처
2. `api.camhr.com/v1.0.0/jobs/simple/page-query` 엔드포인트 발견
3. API를 직접 호출하여 JSON 데이터 수집

## 프로젝트 구조

```
2604PJ_JobCrawler/
├── crawlers/
│   ├── config.py              # 사이트별 크롤링 설정 (CSS 셀렉터 등)
│   ├── gnuboard_crawler.py    # 그누보드 범용 크롤러 (설정 기반)
│   ├── hanin_crawler.py       # 한인회 단일 크롤러 (학습용 초기 버전)
│   └── camhr_crawler.py       # CamHR API 크롤러
├── data/
│   ├── hanin_jobs.json        # 한인회 수집 데이터
│   ├── siemreap_jobs.json     # 시엠립한인회 수집 데이터
│   └── camhr_jobs.json        # CamHR 수집 데이터
├── requirements.txt
├── .gitignore
└── README.md
```

## 설치 및 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# Step 1: 한인회 크롤러 (HTML 파싱)
cd crawlers
python gnuboard_crawler.py

# Step 2: CamHR 크롤러 (API)
python camhr_crawler.py
```

## 크롤링 방식 비교

| | Step 1 (HTML 파싱) | Step 2 (API 크롤링) |
|---|---|---|
| 대상 | 정적 HTML 사이트 | SPA (JavaScript 렌더링) |
| 도구 | requests + BeautifulSoup | requests (API 직접 호출) |
| 데이터 | HTML에서 태그/클래스로 추출 | JSON 응답을 그대로 사용 |
| 속도 | 느림 (HTML 파싱 오버헤드) | 빠름 (필요한 데이터만) |
| 난이도 | 사이트 구조 분석 필요 | API 엔드포인트 찾기 필요 |

## 수집 데이터 형식

### Step 1 (한인회)
```json
{
  "wr_id": "58",
  "title": "2026학년도 프놈펜한국국제학교 중등 체육 시간 강사 모집(재공고)",
  "author": "튜더",
  "date": "2026.02.05",
  "hit": "83",
  "link": "http://www.hanin.or.kr/bbs/board.php?bo_table=Information&wr_id=58",
  "content": "프놈펜한국국제학교에서 아래와 같이...",
  "source": "hanin",
  "crawled_at": "2026-04-14T17:05:08.522835"
}
```

### Step 2 (CamHR)
```json
{
  "id": "10656655",
  "title": "Sales Executive ($1,000 income) + High Bonus",
  "company": "HEALTHY HOMES (CAMBODIA) CO., LTD",
  "cities": "Phnom Penh",
  "salary": "Negotiable",
  "term": "Full Time",
  "is_urgent": true,
  "pub_date": "2026-04-02T00:00:00.000+0700",
  "requirement": "...",
  "description": "...",
  "source": "camhr",
  "crawled_at": "2026-04-14T18:30:00.000000"
}
```

## 기술 스택

- **Python 3.11**
- **requests** - HTTP 요청
- **BeautifulSoup4 + lxml** - HTML 파싱 (Step 1)
- **Playwright** - API 엔드포인트 탐색용 (Step 2)
