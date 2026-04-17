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
- **LLM 전략**: 휴리스틱이 실패한 사이트에 대해 OpenAI(GPT-4o-mini)가 HTML 분석 → 셀렉터 추천. 기본값 `USE_LLM=False` (API 비용 절약); 활성화하려면 [main.py](main.py#L64) 에서 `USE_LLM=True` + `.env` 에 `OPENAI_API_KEY` 설정.

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
# 1. 새 사이트 분석 — URL만 주면 크롤러 설정 자동 생성 (저장 안 함, 출력만)
python main.py analyze <URL>

# 2. 사이트 분석 + 자동 등록 — data/sites.json 에 크롤링 대상으로 추가
python main.py add <URL>

# 3. 등록된 사이트 크롤링 (hardcoded_crawls.py + sites.json 병합)
python main.py crawl

# 4. DB 통계 확인
python main.py stats

# 5. 헬스체크 — 등록된 사이트의 크롤링 건강 상태 리포트
python main.py health

# 6. Slack webhook 연결 검증 — 더미 알림 1회 전송 (SLACK_WEBHOOK_URL 필수)
python main.py notify-test
```

**헬스체크가 감지하는 것** (crawl_runs 이력 기반):
- **에러**: 최근 3회 run 중 exception이 찍힌 run
- **0건**: 가장 최근 run이 신규/재확인 모두 0건 (셀렉터 죽었을 가능성)
- **수집량 급감**: 이번 (신규+재확인) < 이전 평균의 50%
- **스테일**: 마지막 성공이 1일 이상 전 (스케줄러가 안 도는 중)

`crawl`이 끝날 때도 같은 리포트가 자동으로 찍혀서 로그 파일에 남음 — 매일 자동 실행 시 아침에 로그 파일 맨 아래만 확인하면 됨.

### Slack 알림 (선택)

헬스체크 결과를 Slack으로 푸시하려면:

1. Slack에서 **Incoming Webhook** 생성 (https://api.slack.com/messaging/webhooks)
   - App 생성 → Incoming Webhooks ON → 채널 선택 → webhook URL 발급
2. webhook URL을 `.env` 파일에 등록 (프로젝트 루트):
   ```
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../xxx
   ```
   (`.env.example` 파일을 복사해서 쓰면 됨 — `.env`는 `.gitignore` 처리됨)
3. `main.py` 상단에서 `SLACK_ENABLED = True` 로 변경
4. 연결 검증: `python main.py notify-test` — 더미 알림 1회 전송. Slack 채널에 떴으면 성공.

관련 설정:
- `SLACK_ONLY_ISSUES` (기본 True): 문제 있을 때만 알림 / False면 정상 run도 매번 전송
- webhook 전송 실패해도 crawl은 정상 종료 (에러는 로그에만 기록)

> **URL 유출 주의**: webhook URL은 비밀번호와 동급. 실수로 커밋하거나 공유했다면
> 즉시 **Slack App 페이지 → Incoming Webhooks → Regenerate** 로 재발급.

`add`가 등록을 거부하는 경우 (analyzer 신뢰도 부족 / 미지원 사이트 타입 / SPA여서 API 발견 단계 필요 등)
는 콘솔에 이유가 출력된다. 거부된 사이트는 수동 크롤러를 작성한 뒤 [crawlers/hardcoded_crawls.py](crawlers/hardcoded_crawls.py)
의 `REGISTERED_CRAWLS` 에 추가하거나, Playwright 기반 API 자동 발견이 붙을 때까지 대기.

### 매일 자동 실행 (Windows 작업 스케줄러)

`crawl`을 매일 정해진 시간에 자동으로 돌리려면 **Windows 작업 스케줄러**에 등록.
(APScheduler 같은 파이썬 상주 프로세스는 컴퓨터가 꺼지면 죽어서 부적합.)

**실행 결과는 `logs/crawl-YYYYMMDD.log` 에 자동 저장됨** — 백그라운드 실행이라 콘솔 출력이 안 보여도 실행 이력/에러를 확인할 수 있음.

#### 등록 절차

1. **작업 스케줄러** 실행 (`Win + R` → `taskschd.msc`)
2. 우측 패널 → **작업 만들기**
3. **일반** 탭
   - 이름: `JobCrawler Daily`
   - **사용자가 로그온한 경우에만 실행** 선택 (로그아웃 상태 실행은 노트북 환경에선 권장 안 함)
4. **트리거** 탭 → **새로 만들기**
   - 매일 / 시작 시각: 예를 들어 03:00
5. **동작** 탭 → **새로 만들기**
   - 동작: **프로그램 시작**
   - 프로그램/스크립트: 파이썬 실행 파일 전체 경로
     (예: `C:\Users\<사용자>\AppData\Local\Programs\Python\Python311\python.exe` —
     터미널에서 `where python` 으로 확인)
   - 인수 추가: `main.py crawl`
   - 시작 위치: 이 프로젝트의 절대 경로
     (예: `C:\Users\<사용자>\OneDrive\Documents\code\2604PJ_JobCrawler`)
6. **조건** 탭 — 노트북이면 체크 해제 권장
   - **컴퓨터 AC 전원 사용 시에만 작업 시작** 해제 (배터리여도 돌게)
7. 저장 후, 목록에서 해당 작업을 우클릭 → **실행** 으로 수동 트리거해서 정상 동작 확인
8. 몇 분 뒤 `logs/crawl-<오늘날짜>.log` 파일이 생성됐는지 확인

> 참고: 지정 시각에 PC가 꺼져 있으면 해당 날짜 실행은 스킵된다. "작업을 예약대로 시작하지 못한 경우 가능한 한 빨리 작업 시작" 옵션(**설정** 탭)을 켜면 부팅 후 자동으로 밀린 실행을 이어서 돌림.

---

## 향후 계획

| 작업 | 내용 |
|------|------|
| Playwright 기반 API 자동 발견 | Nuxt/React SPA에서 네트워크 캡처로 API 엔드포인트 자동 탐지 |

---

## 기술 스택

- **Python 3.11**
- **requests** — HTTP 요청
- **BeautifulSoup4 + lxml** — HTML 파싱
- **Playwright** — SPA 사이트 네트워크 캡처 / 브라우저 자동화
- **SQLite** — 데이터 저장 (Python 내장)
