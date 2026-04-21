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
| Step 2 | CamHR + 자동화 기반 | REST API 크롤링, DB 증분 수집, **사이트 자동 분석기** | 완료 |
| Step 2.x | 범용 DOM / EmbeddedJSON / API 추출 | 3가지 추출 경로 통합, validator + LLM retry | 완료 |
| Step 3 | 자동 크롤링 파이프라인 | Task Scheduler + Slack 알림 + 헬스체크 | 진행 중 |

---

## 크롤링 대상

현재 9개 사이트 등록 (2026-04 기준).

| site_id | 추출 방식 | URL | 비고 |
|---------|-----------|-----|------|
| hanin | dom | http://www.hanin.or.kr | 재캄보디아한인회 (그누보드 nariya) |
| siemreap | dom | https://siemreap.korean.net | 시엠립한인회 (그누보드 fz) |
| camhr | api | https://www.camhr.com | 캄보디아 최대, Nuxt SSR → API 직접 호출 |
| jobkorea | dom | https://www.jobkorea.co.kr/recruit/joblist | |
| incruit | dom | https://job.incruit.com/jobdb_list/searchjob.asp | `today=y` 파라미터로 당일만 |
| wanted | api | https://www.wanted.co.kr/wdlist | Phase 3 api_crawler 자동 등록 |
| ppomppu | dom | https://www.ppomppu.co.kr/zboard/zboard.php?id=guin | |
| alba | dom | https://www.alba.co.kr/job/Main | |
| radiokorea | dom | https://www.radiokorea.com/community/jobs.php | 교민 커뮤니티 |

> 목록은 `python scripts/list_sites.py` 로 확인. 비활성화된 사이트는 `enabled: false` 로 건너뜀.

---

## 아키텍처

### 전체 구조

```
┌───────────────────────────────────────────────────────────────┐
│                           main.py                             │
│      (엔트리포인트 / [SETTINGS] / CLI: analyze/add/crawl/...) │
└───┬───────────────┬──────────────────┬──────────────────┬─────┘
    ▼               ▼                  ▼                  ▼
┌─────────┐   ┌──────────────┐   ┌─────────────┐   ┌──────────┐
│analyzer │   │ dispatcher   │   │ healthcheck │   │ database │
│  (분석) │   │  (crawl 분기)│   │  (이상 감지)│   │(SQLite)  │
└────┬────┘   └──────┬───────┘   └──────┬──────┘   └─────┬────┘
     │               │                  │                │
     ▼               ▼                  ▼                ▼
  URL → config   dom / api /       crawl_runs 읽음   jobs.db
  (4 strategy)   embedded_json     → OK/WARN/ERROR
                 크롤러 중 선택    → slack_notifier
```

### 1. 사이트 자동 분석기 (Analyzer)

새 사이트를 추가할 때 **URL만 주면 크롤러 설정(config)을 자동 생성**해주는 모듈.
1,500개 사이트를 수동 분석할 수 없으므로 이 단계의 자동화가 핵심.

**4-단계 전략 체인 (Strategy Pattern, main.py 에서 on/off):**

1. **Heuristic** — 플랫폼 시그니처 탐지 (그누보드 전역변수, `__NUXT__`, `__NEXT_DATA__`, `wp-content`), 알려진 테마의 CSS 셀렉터 매칭. 상세링크 패턴에서 `external_id` 파라미터 자동 감지.
2. **PlaywrightDiscovery** (SPA 한정) — 헤드리스 Chromium 으로 페이지를 띄워 **네트워크 트래픽에서 내부 API 엔드포인트를 스니핑**. 규칙 점수 상위 후보를 LLM 랭커에 넘겨 "진짜 공고 리스트 API" 재선별 (메타데이터/필터옵션 API 필터링).
3. **EmbeddedJSON** — `#__NEXT_DATA__` / `window.__NUXT__` 같은 전역 state 에서 SSR 데이터 추출. Playwright 렌더 경로 포함 (HTML-only 로 못 찾는 factory form CSR 대응).
4. **LLM** — 앞 전략이 전부 실패했을 때 OpenAI(GPT-4o-mini) 가 HTML 분석 → 셀렉터 추천. 비용 최후의 보루.

**분석 결과는 validator 로 실제 검증** — 샘플 제목을 뽑아 config 가 동작하는지 확인. 실패하면 LLM 에 실패 사유와 함께 **selectors 를 고쳐달라고 retry** (최대 2회, `VALIDATOR_RETRY_MAX` 로 조정).

**자동 생성되는 3가지 extraction_method:**

| method | 대상 사이트 | 크롤러 모듈 |
|--------|-------------|-------------|
| `dom` | 정적 HTML (그누보드, 일반 리스트 페이지) | [crawlers/dom_crawler.py](crawlers/dom_crawler.py) |
| `embedded_json` | SSR state 가 HTML 에 박힌 Next/Nuxt | [crawlers/embedded_crawler.py](crawlers/embedded_crawler.py) |
| `api` | REST API 가 드러난 SPA (CamHR, Wanted) | [crawlers/api_crawler.py](crawlers/api_crawler.py) |

### 2. 크롤러 + Dispatcher

[crawlers/dispatcher.py](crawlers/dispatcher.py) 가 site entry 의 `extraction_method` 를 보고 위 3개 크롤러 중 하나로 분기한다. 모든 크롤러는 **DB 에 증분 저장** + `crawl_runs` 이력 row 를 남긴다.

**조기 종료 최적화**: 크롤링 중 연속으로 이미 저장된 공고만 만나면 "더 과거로 페이징해봤자 전부 재확인뿐" 이라고 보고 멈춘다 — 증분 수집에서 서버 부담과 시간을 크게 줄임.

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
├── main.py                       # 엔트리포인트 ([SETTINGS] 섹션 상단에 모여있음)
│
├── crawlers/
│   ├── analyzer/                 # 사이트 자동 분석기
│   │   ├── analyzer.py           # 오케스트레이터
│   │   ├── models.py             # 결과 데이터 클래스
│   │   ├── validator.py          # config 실동작 검증 (샘플 제목 추출)
│   │   └── strategies/           # 4-단계 전략 체인
│   │       ├── base.py
│   │       ├── heuristic.py      # 플랫폼 시그니처 + 알려진 테마
│   │       ├── playwright_discovery.py  # SPA 내부 API 스니핑
│   │       ├── embedded_json.py  # __NEXT_DATA__ / __NUXT__
│   │       └── llm.py            # OpenAI 폴백 + selector retry
│   │
│   ├── dispatcher.py             # extraction_method 보고 크롤러 선택
│   ├── dom_crawler.py            # 정적 HTML (그누보드 포함)
│   ├── embedded_crawler.py       # SSR state 추출형 SPA
│   ├── api_crawler.py            # REST API 직접 호출 (CamHR, Wanted)
│   ├── hardcoded_crawls.py       # analyzer 자동등록 불가 사이트 수동 정의
│   ├── sites_registry.py         # sites.json CRUD + 중복 체크
│   ├── http_client.py            # requests + 재시도 + 공통 헤더
│   ├── database.py               # SQLite (jobs / crawl_runs)
│   ├── healthcheck.py            # crawl_runs 이력 기반 이상 감지
│   └── slack_notifier.py         # 헬스체크 알림 + 배치 요약 알림
│
├── scripts/
│   ├── run_crawl.bat             # Task Scheduler 엔트리 (cwd+로그 래퍼)
│   ├── list_sites.py             # 등록된 사이트 목록 출력
│   ├── check_today_runs.py       # 오늘 crawl_runs 결과 확인
│   └── ... (진단/탐색 스크립트 다수)
│
├── data/
│   ├── jobs.db                   # SQLite (jobs + crawl_runs)
│   └── sites.json                # `add` 로 등록된 동적 사이트들
│
└── logs/
    ├── crawl-YYYYMMDD.log        # cmd_crawl 내부에서 tee
    └── scheduled-YYYYMMDD.log    # run_crawl.bat 래퍼가 stdout 캡처
```

---

## 사용 방법

### 설정 변경
`main.py` 상단의 `[SETTINGS]` 섹션에서 조정:
- `USE_LLM` (기본 True) — analyzer LLM 폴백. `OPENAI_API_KEY` 필요
- `USE_PLAYWRIGHT_DISCOVERY` (기본 True) — SPA 내부 API 자동 스니핑
- `USE_PLAYWRIGHT_RENDER` (기본 True) — `__NUXT__` 같은 CSR state 뽑기
- `USE_LLM_API_RANKER` (기본 True) — 후보 API 중 "진짜 리스트 API" LLM 재선별
- `VALIDATOR_RETRY_MAX` (기본 2) — validator 실패 시 LLM 에 selector 수정 요청 횟수
- `HTTP_TIMEOUT` / `HTTP_MAX_RETRIES` / `HTTP_RETRY_BACKOFF`
- `SLACK_ENABLED`, `SLACK_CRAWL_SUMMARY`, `SLACK_ONLY_ISSUES`

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

### Slack 알림

`crawl` 이 끝나면 **두 종류의 Slack 메시지**가 독립적으로 날아간다:

1. **배치 요약** (`SLACK_CRAWL_SUMMARY`, 기본 True) — 매 run 전송. 사이트별 신규/재확인 건수, 실패 사이트는 에러 메시지까지 한 메시지로. `크롤 완료: 7/7 성공 · 소요 1:12 · 신규 70 / 재확인 291` 같은 헤더.
2. **헬스체크 알림** (`SLACK_ONLY_ISSUES`, 기본 True) — `crawl_runs` 이력 기반으로 **문제가 감지될 때만** 전송 (정상일 때는 조용). 스케줄러가 매일 돌면 3-run 윈도우로 추세 감지.

설정 방법:

1. Slack에서 **Incoming Webhook** 생성 (https://api.slack.com/messaging/webhooks)
2. webhook URL을 `.env` 파일에 등록 (프로젝트 루트):
   ```
   SLACK_WEBHOOK_URL=https://hooks.slack.com/services/T.../B.../xxx
   ```
   (`.env.example` 파일을 복사해서 쓰면 됨 — `.env`는 `.gitignore` 처리됨)
3. `main.py` 상단에서 `SLACK_ENABLED = True` 로 변경
4. 연결 검증: `python main.py notify-test` — 더미 알림 1회 전송. Slack 채널에 떴으면 성공.

webhook 전송 실패해도 crawl은 정상 종료 (에러는 로그에만 기록).

> **URL 유출 주의**: webhook URL은 비밀번호와 동급. 실수로 커밋하거나 공유했다면
> 즉시 **Slack App 페이지 → Incoming Webhooks → Regenerate** 로 재발급.

`add`가 등록을 거부하는 경우 (analyzer 신뢰도 부족 / 미지원 사이트 타입 / SPA여서 API 발견 단계 필요 등)
는 콘솔에 이유가 출력된다. 거부된 사이트는 수동 크롤러를 작성한 뒤 [crawlers/hardcoded_crawls.py](crawlers/hardcoded_crawls.py)
의 `REGISTERED_CRAWLS` 에 추가하거나, Playwright 기반 API 자동 발견이 붙을 때까지 대기.

### 매일 자동 실행 (Windows 작업 스케줄러)

`crawl`을 매일 정해진 시간에 자동으로 돌리려면 **Windows 작업 스케줄러**에 등록.
(APScheduler 같은 파이썬 상주 프로세스는 컴퓨터가 꺼지면 죽어서 부적합.)

래퍼 스크립트로 [scripts/run_crawl.bat](scripts/run_crawl.bat) 를 제공한다 — cwd 를 맞추고, Python 절대경로로 `main.py crawl` 을 실행한 뒤, 결과를 `logs/scheduled-YYYYMMDD.log` 에 append 한다. 스케줄러에서는 이 bat 하나만 등록하면 됨.

#### PowerShell 로 한 번에 등록 (권장)

```powershell
$action   = New-ScheduledTaskAction -Execute "C:\Users\<사용자>\OneDrive\Documents\code\2604PJ_JobCrawler\scripts\run_crawl.bat"
$trigger  = New-ScheduledTaskTrigger -Daily -At 00:00
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 1) `
    -DontStopIfGoingOnBatteries `
    -AllowStartIfOnBatteries
Register-ScheduledTask -TaskName "JobCrawler" -Action $action -Trigger $trigger -Settings $settings
```

옵션 의미:
- `-StartWhenAvailable` — 00:00 에 PC 가 꺼져 있었으면 켜진 직후 자동 catch-up
- `-MultipleInstances IgnoreNew` — 이전 run 이 아직 돌고 있으면 새 run 스킵 (DB lock 방지)
- `-ExecutionTimeLimit 1h` — 1시간 넘으면 강제 종료 (무한루프/행 방지)

#### GUI 등록

1. `Win + R` → `taskschd.msc` → **작업 만들기**
2. **동작** 탭 → **프로그램 시작** → `scripts/run_crawl.bat` 의 절대경로
3. **트리거** 탭 → 매일 / 00:00
4. **설정** 탭 → "예약대로 시작하지 못한 경우 가능한 한 빨리 작업 시작" 체크, "작업을 중지하기까지 시간" = 1시간
5. **조건** 탭 → (노트북이면) "컴퓨터 AC 전원 사용 시에만 작업 시작" 해제
6. 저장 후 목록에서 우클릭 → **실행** 으로 수동 트리거해서 동작 확인. `logs/scheduled-<오늘>.log` 가 생성되면 성공.

#### 결과 확인

- Slack 알림 — 배치 요약이 바로 날아옴 (설정했다면)
- `logs/scheduled-YYYYMMDD.log` — 전체 stdout/stderr
- `python scripts/check_today_runs.py` — 오늘 crawl_runs rows 조회

---

## 향후 계획

| 작업 | 내용 |
|------|------|
| 1,500 사이트 확장 | analyzer 신뢰도 재튜닝 + `python main.py add <URL>` 배치 등록 |
| 사이트 전원 재시도 격리 | 단일 사이트 타임아웃이 배치 전체 ExecutionTimeLimit 을 먹지 않게 per-site timeout |
| WordPress / 기타 플랫폼 | 현재 heuristic 은 구조만 준비 — 실제 샘플로 selectors 확정 필요 |

---

## 기술 스택

- **Python 3.11**
- **requests** — HTTP 요청
- **BeautifulSoup4 + lxml** — HTML 파싱
- **Playwright** — SPA 사이트 네트워크 캡처 / 브라우저 자동화
- **SQLite** — 데이터 저장 (Python 내장)
