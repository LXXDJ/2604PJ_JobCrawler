# JobCrawler - 구인구직 사이트 크롤링 프로젝트

캄보디아 교민 대상 구인구직 사이트를 크롤링하며 웹 크롤링 기술을 단계적으로 학습하는 프로젝트.

## 프로젝트 목표

- 정적/동적 크롤링 기법 학습
- 봇 탐지 우회 방법 습득
- 매일 자동 증분 수집 파이프라인 구축

## 진행 단계

| 단계 | 대상 사이트 | 학습 내용 | 상태 |
|------|------------|-----------|------|
| Step 1 | 재캄보디아한인회 | 정적 크롤링 (requests + BeautifulSoup) | 완료 |
| Step 2 | CamHR | 봇 탐지 우회 (Scrapling/Playwright), DB 저장, 증분 수집 | 예정 |

## 크롤링 대상

### Step 1 - 재캄보디아한인회 구인구직 게시판

- URL: http://www.hanin.or.kr/bbs/board.php?bo_table=Information
- 플랫폼: 그누보드 (나리야 테마)
- 봇 탐지: 없음 (robots.txt 미존재)
- 수집 항목: 게시글 번호, 제목, 작성자, 등록일, 조회수, 본문 내용

### Step 2 - CamHR (예정)

- URL: https://www.camhr.com/
- 캄보디아 최대 구인구직 사이트

## 프로젝트 구조

```
2604PJ_JobCrawler/
├── crawlers/
│   └── hanin_crawler.py    # 재캄보디아한인회 크롤러
├── data/
│   └── hanin_jobs.json     # 수집된 데이터
├── requirements.txt
├── .gitignore
└── README.md
```

## 설치 및 실행

```bash
# 의존성 설치
pip install -r requirements.txt

# 한인회 크롤러 실행
python crawlers/hanin_crawler.py
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
  "crawled_at": "2026-04-14T17:05:08.522835"
}
```

## 기술 스택

- **Python 3.11**
- **requests** - HTTP 요청
- **BeautifulSoup4 + lxml** - HTML 파싱
- (Step 2 예정) Scrapling, Playwright - 봇 탐지 우회 / 동적 페이지 처리
- (Step 2 예정) SQLite - 증분 수집용 DB
