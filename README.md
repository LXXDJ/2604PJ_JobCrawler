# JobCrawler - 구인구직 사이트 크롤링 프로젝트

캄보디아 교민 대상 구인구직 사이트를 크롤링하며 웹 크롤링 기술을 단계적으로 학습하는 프로젝트.
최종 목표는 **1,500개 사이트**를 자동 크롤링하는 확장 가능한 파이프라인 구축.

---

## 프로젝트 목표

- 정적/동적 크롤링 기법 학습
- 봇 탐지 우회 방법 습득
- 매일 자동 증분 수집 파이프라인 구축
- **사이트 구조를 자동 분석해서 크롤러 설정을 생성하는 시스템** (대규모 확장 대비)

---

## 진행 단계

| 단계 | 주제 | 학습 포인트 | 상태 |
|------|------|-------------|------|
| Step 1 | 한인회 / 시엠립 (그누보드) | HTML 파싱, 설정 기반 멀티사이트 크롤러 | 완료 |
| Step 2 | CamHR + 자동화 기반 구축 | API 크롤링, DB 증분 수집, **사이트 자동 분석기** | 진행 중 |

---

## 크롤링 대상

### Step 1 — 한인 커뮤니티 (그누보드)
- 재캄보디아한인회: http://www.hanin.or.kr (nariya 테마)
- 시엠립한인회: https://siemreap.korean.net (fz 테마)
- 같은 그누보드지만 테마 차이로 HTML 구조가 다름 → **설정 분리 + 범용 크롤러**로 해결

### Step 2 — CamHR
- https://www.camhr.com/
- 캄보디아 최대 구인구직 사이트 (1,500+ 공고)
- Nuxt.js(Vue SSR) 기반 SPA → HTML에 데이터 없음 → **REST API 직접 호출** 방식으로 크롤링

---

## 아키텍처

### 전체 구조

```
┌─────────────────────────────────────────────────────────┐
│                       main.py                           │
│   (엔트리포인트 / 사용자 설정 / CLI)                     │
└──────────────────┬──────────────────────────────────────┘
                   │
       ┌───────────┼────────────┐
       ▼           ▼            ▼
  ┌─────────┐ ┌─────────┐ ┌──────────┐
  │analyzer │ │crawlers │ │ database │
  │ (분석기)│ │ (수집)  │ │  (저장)  │
  └─────────┘ └─────────┘ └──────────┘
       │           │            │
       ▼           ▼            ▼
   URL 분석     실제 크롤링    SQLite
   → config     → JSON        (jobs.db)
                → DB upsert
```

### 1. 사이트 자동 분석기 (Analyzer)

새 사이트를 추가할 때 **URL만 주면 크롤러 설정(config)을 자동 생성**해주는 모듈.
1,500개 사이트를 수동 분석할 수 없으므로 이 단계의 자동화가 핵심.

**하이브리드 접근 (Strategy Pattern):**

```
분석 전략 1: 휴리스틱 (기본, 무료, 빠름)
  ↓ (실패 시 폴백)
분석 전략 2: LLM (옵션, 현재 비활성화)
```

- **휴리스틱 전략**: HTML에서 플랫폼 시그니처 탐지 (그누보드 전역변수, `__NUXT__`, `wp-content` 등), 알려진 테마의 CSS 셀렉터 매칭
- **LLM 전략**: 휴리스틱이 실패한 사이트에 대해 Claude가 HTML 분석 → 셀렉터 추천 (현재 스텁만 존재, `USE_LLM=False`로 비활성)

**지원하는 사이트 타입:**
| 타입 | 식별 방법 | 자동 생성되는 config |
|------|-----------|---------------------|
| `GNUBOARD` | `g5_bbs_url`, `/bbs/board.php` 시그니처 | base_url, bo_table, 테마별 CSS 셀렉터 |
| `SPA_NUXT` | `__NUXT__`, `/_nuxt/` 시그니처 | base_url, API base 후보 URL |
| `SPA_NEXT` | `__NEXT_DATA__` | (구조만 준비) |
| `WORDPRESS` | `wp-content/` | (구조만 준비) |

### 2. 크롤러 (Crawlers)

사이트 타입별로 크롤러가 존재하고, 모두 **DB에 증분 저장**한다.

- **그누보드 크롤러** (Step 1): requests + BeautifulSoup, 설정 기반 멀티사이트
- **CamHR 크롤러** (Step 2): API 직접 호출, 목록+상세 2단계 수집

### 3. 데이터베이스 (Database)

**SQLite 단일 파일 DB + 단일 `jobs` 테이블**

| 선택 이유 | 설명 |
|-----------|------|
| SQLite | 서버 설치 불필요, 파일 하나로 관리, 소규모~중규모에 충분 |
| 단일 테이블 | 1,500개 사이트에 사이트별 테이블은 관리 불가능. `source` 컬럼으로 구분 |
| `(source, external_id)` UNIQUE | 같은 공고 중복 방지 |
| `first_seen_at` / `last_seen_at` | 증분 수집 추적 |
| `raw_data` JSON 컬럼 | 사이트마다 다른 필드는 JSON에 통째로 저장 (확장성) |

### 4. 증분 수집

**매일 돌리면 신규 공고만 쌓이는 구조:**

```
1. 목록 API/페이지에서 현재 공고 리스트 조회
2. 각 공고를 (source, external_id)로 DB 조회
   - 없으면 INSERT (신규)
   - 있으면 last_seen_at만 UPDATE (재확인)
3. 크롤링 실행 이력을 crawl_runs 테이블에 기록
```

**장점:**
- 이미 수집한 공고의 상세 API 호출을 스킵 → 빠르고 서버에 덜 부담
- 공고가 사라진 경우(만료)는 last_seen_at이 오래된 것으로 구분 가능

### 5. HTTP 안정성 (재시도 로직)

- 모든 HTTP 요청은 **기본 3회 재시도** + **점증적 대기(backoff)**
- 일시적 타임아웃, 네트워크 오류에 견고
- `main.py` 상단의 `HTTP_TIMEOUT`, `HTTP_MAX_RETRIES`, `HTTP_RETRY_BACKOFF`로 조정

---

## 디렉토리 구조

```
2604PJ_JobCrawler/
├── main.py                       # 엔트리포인트 (모든 사용자 설정 상단에 모여있음)
│
├── crawlers/
│   ├── analyzer/                 # 사이트 자동 분석기
│   │   ├── analyzer.py           # 오케스트레이터
│   │   ├── models.py             # 결과 데이터 클래스
│   │   └── strategies/           # 전략 패턴
│   │       ├── base.py           # 전략 인터페이스
│   │       ├── heuristic.py      # 휴리스틱 (구현됨)
│   │       └── llm.py            # LLM 스텁 (비활성)
│   │
│   ├── camhr_crawler.py          # CamHR API 크롤러
│   └── database.py               # SQLite DB 관리
│
└── data/
    └── jobs.db                   # SQLite 데이터베이스
```

---

## 사용 방법

### 설정 변경
`main.py` 상단의 `[SETTINGS]` 섹션에서 조정:
- LLM 사용 여부, API 키, 모델
- HTTP 타임아웃 / 재시도
- 크롤링 페이지 수, DB 경로 등

### 명령어

```bash
# 1. 새 사이트 분석 — URL만 주면 크롤러 설정 자동 생성
python main.py analyze <URL>

# 2. 등록된 사이트 크롤링
python main.py crawl

# 3. DB 통계 확인
python main.py stats
```

---

## 향후 계획

| 작업 | 내용 |
|------|------|
| Playwright 기반 API 자동 발견 | Nuxt/React SPA에서 네트워크 캡처로 API 엔드포인트 자동 탐지 |
| 분석기 → 크롤러 자동 연동 | analyze 결과 config를 바로 crawl에 전달 |
| 헬스체크 | 주기 실행 시 수집 건수 급감, 구조 변경 등 자동 감지 |
| LLM 전략 활성화 | 휴리스틱이 실패한 사이트에 대해 Claude 폴백 |
| 스케줄러 | APScheduler 또는 cron으로 매일 자동 실행 |

---

## 기술 스택

- **Python 3.11**
- **requests** — HTTP 요청
- **BeautifulSoup4 + lxml** — HTML 파싱
- **Playwright** — SPA 사이트 네트워크 캡처 / 브라우저 자동화
- **SQLite** — 데이터 저장 (Python 내장)
