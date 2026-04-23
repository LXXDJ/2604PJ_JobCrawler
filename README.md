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
| Step 3 | 자동 크롤링 파이프라인 | Task Scheduler + Slack 알림 + 헬스체크 | 완료 |
| Step 3.5 | **봇차단 / 방어 패턴 돌파** | curl_cffi(JA3) · LLM Ranker 재시도 · SPA post-hoc recovery · 2단계 중첩 `[*]` · validator 2차 시그널 | 완료 |

---

## 크롤링 대상

현재 **17개 사이트 등록** (2026-04-21 기준). 9개 → 17개 확장 과정에서 사이트별 방어 강도가 크게 달라 **난이도별로 분류**하고, 각 단계에서 도입한 우회 기법을 정리함.

> 목록은 `python scripts/inspect/list_sites.py` 로 확인. 비활성화된 사이트는 `enabled: false` 로 건너뜀.

### 난이도 분류 요약

| 난이도 | 특징 | 필요한 기법 | 사이트 수 |
|--------|------|-------------|-----------|
| **하** | 정적 HTML, 봇차단 없음 | `requests` + CSS selector (heuristic 자동) | 11 |
| **중** | SPA, JSON API 뒤에 목록 숨김 | Playwright 로 내부 API 스니핑 + LLM Ranker | 5 |
| **상** | JA3/TLS 지문 탐지로 403 | curl_cffi (Chrome TLS impersonate) | 1 |
| 미해결 | OpenAPI 전용 / 인터랙티브 | 추가 엔지니어링 or 별도 경로 | 2~ |

---

### 하 — 정적 HTML / 그누보드 (11개)

**공통 특징**: 초기 HTML 응답에 공고 리스트가 그대로 박혀있음. User-Agent 만 바꾸면 그냥 긁힘.
**도입 기법**: `heuristic.py` 의 플랫폼 시그니처 탐지 (그누보드 전역변수, 알려진 테마 CSS 셀렉터). 실패하면 LLM 에 selector 추천 요청.

| site_id | URL | 서브타입 | 한 줄 설명 |
|---------|-----|----------|-----------|
| hanin | http://www.hanin.or.kr | 그누보드 (nariya) | 재캄보디아한인회 |
| siemreap | https://siemreap.korean.net | 그누보드 (fz) | 시엠립한인회 |
| ppomppu | https://www.ppomppu.co.kr/zboard/zboard.php?id=guin | 그누보드 (zboard) | 뽐뿌 구인정보 |
| radiokorea | https://www.radiokorea.com/community/jobs.php | 그누보드 (custom) | LA 교민 |
| jobkorea | https://www.jobkorea.co.kr/recruit/joblist | static_html | — |
| incruit | https://job.incruit.com/jobdb_list/searchjob.asp | static_html | `today=y` 파라미터로 당일만 |
| alba | https://www.alba.co.kr/job/Main | static_html | 알바천국 |
| peoplenjob | https://www.peoplenjob.com/jobs | static_html | 피플앤잡 (외국계 전문) |
| career | https://job.career.co.kr/jobs/ | static_html | 커리어 |
| hibrain | hibrain.net/recruitment/categories/JOB/categories/EXP/recruits | static_html | 하이브레인 (연구/박사급) |

---

### 중 — SPA 내부 API 스니핑 (6개)

**공통 특징**: 초기 HTML 은 빈 껍데기 (React/Nuxt/Next.js). 공고 리스트는 JS 가 나중에 `/api/...` 를 호출해서 채움. HTML 만 긁으면 0건.
**도입 기법**:
1. `playwright_discovery.py` — 헤드리스 Chromium 으로 페이지를 실제로 띄워 **네트워크 트래픽을 가로채** `/api/*.json` 후보 수집
2. 규칙 점수(응답 크기·JSON 배열 길이·경로 패턴) 상위를 **LLM Ranker** 에 넘겨 "메타데이터/필터옵션 API 말고 진짜 공고 리스트" 재선별
3. validator 가 응답 구조 검증 (제목/회사/URL 등 2차 시그널 + 2단계 중첩 자동 탐지 `[*]` 구문)
4. 실패하면 LLM Ranker 재시도 루프 (`exclude` 로 이전 pick 제외)

| site_id | URL | API 엔드포인트 | 특기사항 |
|---------|-----|----------------|----------|
| camhr | https://www.camhr.com | `api.camhr.com/.../page-query` | 수동 polish (`urgent=true` 필터 제거, `size=50`, `detail_endpoint_template` 추가) |
| wanted | https://www.wanted.co.kr/wdlist | `/api/chaos/navigation/v1/results` | `wanted-user-agent: user-web` 헤더 필수 |
| rocketpunch | https://www.rocketpunch.com/jobs | `/api/proxy/jobs` | `x-rocket-client-id`, `x-rocket-app-key` 등 8종 커스텀 헤더 |
| jumpit | https://www.jumpit.co.kr/positions | `jumpit-api.saramin.co.kr/api/positions` | 사람인 계열 (도메인만 다름) |
| jobplanet | https://www.jobplanet.co.kr/job | `/api/v3/job/postings` | `jp-ssr-auth` 토큰 (고정값) + `jp-os-type: mobile_web` |
| findall | https://www.findall.co.kr/job | `findjob.co.kr/.../mainPartTimeJobList` | **2단계 중첩** 응답 — validator 가 `data.partTimeJobList[*].jobAdList` 로 item_path 자동 업그레이드 |

> **링커리어·알바몬 제외 이유** (역사적): LLM Ranker 가 GraphQL 필터옵션 / 브랜드 코드 API 를 1위로 오인식해서 `scripts/maintenance/cleanup_bad_entries.py` 로 제거했었음. 이후 아래 ① + ② 로 대응 완료.
>
> **① validator 강화** ([validator.py:27-58](crawlers/analyzer/validator.py#L27-L58)): `validate_api_config` 에 3가지 2차 시그널 체크 추가 — "`name` 필드 있는 배열" 만으로는 통과 못 함.
> - 제목 평균 길이 ≥ 8자 (필터코드는 2~6자)
> - 2차 시그널 (회사·날짜·위치·URL·급여 등) 보유 아이템 ≥ 50%
> - `{id,code,name,value,label}` 3개 이하 필드로만 구성된 아이템이 80%+ 면 "코드테이블" 로 거부
>
> **② LLM Ranker 재시도 루프** ([main.py cmd_add API 분기](main.py) + [playwright_discovery.py `llm_rank_candidates`](crawlers/analyzer/strategies/playwright_discovery.py)): validator 가 거부하면 해당 idx 를 exclude 에 추가하고 LLM 에 다른 후보 요청 (최대 `VALIDATOR_RETRY_MAX` 회). pool 은 `result.config["_ranker_pool"]` 로 전략이 보존 — 언더스코어 prefix 는 sites.json 에 저장 안 함.
>
> **아직 수동 필요 케이스 (5~10% 추정)**: 로그인 후에만 진짜 API 가 호출 / 복잡한 POST body / 스크롤/필터 클릭 같은 상호작용이 있어야 API 가 노출 / iframe 격리. 현재 링커리어가 이 범주 — pool 안에 진짜 공고 API 가 아예 없음.

---

### 상 — JA3/TLS 지문 우회 (1개)

**공통 특징**: User-Agent·Accept·Sec-Fetch-* 를 Chrome 과 동일하게 보내도 **403 Forbidden**. 이유는 **TLS 핸드셰이크 패턴(JA3 지문)** 이 Python/OpenSSL 고유값이라 서버가 보자마자 봇 판정.

**도입 기법**: [crawlers/http_client.py](crawlers/http_client.py) 를 `requests` → **curl_cffi** 로 교체.
- 내부적으로 C 라이브러리 **curl-impersonate** 를 쓰는 Python 바인딩
- `impersonate="chrome131"` 한 줄로 실제 Chrome 131 의 암호 스위트·TLS 확장·HTTP/2 프레임 순서까지 그대로 재현
- 크롤러·분석기 전체가 `http_client.fetch` 하나를 쓰므로 한 곳만 바꾸면 모든 호출이 JA3 우회

```python
# crawlers/http_client.py
from curl_cffi import requests as cffi_requests

response = cffi_requests.get(url, impersonate="chrome131", headers=..., ...)
```

| site_id | URL | 증상 | 해결 |
|---------|-----|------|------|
| saramin | https://www.saramin.co.kr/.../job-category | `requests` 로 403 즉시 차단 | curl_cffi chrome131 impersonate → 78건 추출 |

---

### 미해결 — 추가 엔지니어링 필요

발견은 했지만 현재 파이프라인으로 못 잡은 사이트들. 방어 패턴별로 묶어 기록.

#### ① Form-submit / OpenAPI 전용 (스크래핑 비권장)

검색 버튼 클릭/로그인 이후에만 공고가 표시되는 인터랙티브 사이트. 분석 결과 두 사이트 모두 **공공기관 OpenAPI 를 정식 경로로 제공** — 스크래핑보다 API 키 발급 쪽이 정석.

| 사이트 | 권장 경로 |
|--------|-----------|
| 고용24 (work24) | [고용24 OpenAPI](https://www.work24.go.kr/cm/openApi/openApiInfoView.do) — 키 발급 후 REST 호출 |
| 알리오 (alio) | 공공데이터포털 [공공기관 채용정보 API](https://www.data.go.kr/) — 키 발급 필요 |

→ 스크래핑 파이프라인 범위 밖. 필요하면 `hardcoded_crawls.py` 에 OpenAPI 호출 어댑터 수동 등록.

#### ② SPA 뒤늦은 감지 (LLM fallback → post-hoc recovery)

**실제 원인**: 애초에 `SITE_TYPE_TO_EXTRACTION_METHOD` 에 매핑이 없는 게 문제가 아니라, heuristic 이 명시적 SPA 마커(`window.__NUXT__=`, `/_nuxt/`, `id="__NEXT_DATA__"`)를 못 찾아 playwright_discovery 가 **아예 실행되지 않는** 것이 진짜 원인. 그 다음 LLM 이 뒤늦게 "빈 HTML = SPA" 라고 추측해도 이미 playwright 기회는 지나감.

**도입 기법**: [analyzer.py `_recover_spa_with_playwright`](crawlers/analyzer/analyzer.py) — 최종 결과가 `SPA_*` 타입이고 heuristic 이 SPA 로 확정하지 않았던 케이스에서 **playwright_discovery 를 retroactively 재호출**. LLM 의 SPA 추측을 버리지 않고 실제 네트워크 캡처 기회를 한 번 더 준다.

| 사이트 | 상태 | 비고 |
|--------|------|------|
| 벼룩시장 (findall) | **등록 완료** — 18건 수집 | 2단계 중첩 구조 (`data.partTimeJobList[*].jobAdList`) 를 validator 가 자동 탐지해 item_path 를 `[*]` 구문으로 업그레이드. `_traverse_path` 양쪽(validator + api_crawler)에 와일드카드 지원 추가 |
| 스카우트 (scout) | recovery 작동하나 Playwright 가 XHR 캡처 0건 | 리스트 페이지가 스크롤/클릭 같은 상호작용 후에만 XHR 발생하는 타입 — 인터랙티브 Playwright 필요 (별건 과제) |

#### ③ 쿠키/세션 기반 심화 방어 — **해결됨**

JA3 지문까지 흉내내도 막히는 사이트. curl_cffi 적용 이후 재확인해보니 하이브레인은 이제 정상 통과. 실제 문제는 "**잘못된 URL**" 이었음 — `/jobs` 페이지가 카테고리 내비게이션 허브였고 진짜 공고는 `/recruitment/categories/JOB/categories/EXP/recruits` 같은 서브 URL 에 있음.

| 사이트 | 상태 | 비고 |
|--------|------|------|
| 하이브레인 (hibrain) | **등록 완료** — 228건 수집 | URL 을 `/recruitment/categories/JOB/categories/EXP/recruits` (경력자 카테고리) 로 지정 |

→ 교훈: "봇차단으로 보이는 403" 이 실제로는 URL 이 의미 없는 네비게이션 페이지라 공고 selectors 가 안 맞는 경우가 있음. curl_cffi 정상 동작 확인 후 사이트 구조 재탐색 필요.

#### ④ 로컬 네트워크 이슈

| 사이트 | 증상 |
|--------|------|
| 프로그래머스 (programmers) | DNS 해석 실패 — 코드 문제 아님 |

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

**SPA post-hoc recovery** ([analyzer.py `_recover_spa_with_playwright`](crawlers/analyzer/analyzer.py)): heuristic 이 명시적 SPA 마커(`__NUXT__`, `/_nuxt/`)를 못 잡아도 LLM 이 "빈 HTML = SPA" 로 뒤늦게 판정하면 **playwright_discovery 를 retroactively 재호출**. 벼룩시장(findall) 처럼 마커 없는 SPA 대응.

**자동 생성되는 3가지 extraction_method:**

| method | 대상 사이트 | 크롤러 모듈 |
|--------|-------------|-------------|
| `dom` | 정적 HTML (그누보드, 일반 리스트 페이지) | [crawlers/dom_crawler.py](crawlers/dom_crawler.py) |
| `embedded_json` | SSR state 가 HTML 에 박힌 Next/Nuxt | [crawlers/embedded_crawler.py](crawlers/embedded_crawler.py) |
| `api` | REST API 가 드러난 SPA (CamHR, Wanted, findall 등) | [crawlers/api_crawler.py](crawlers/api_crawler.py) |

#### Validator — LLM 환각 방어선

`analyzer` 가 생성한 config 를 sites.json 에 저장하기 전에 **실제로 돌려본다**. 통과 못 하면 저장 거부 — "프로덕션에서 0건 수집" 같은 침묵 실패 방지.

- **DOM validator**: list_rows 매칭 ≥ 2, subject_link 로 제목 뽑힌 비율 ≥ 50%, UI 노이즈(로그인/검색 등)만 잡히면 거부
- **API validator**: HTTP 200 + item_path 배열 길이 ≥ 2 + 제목성 필드 비율 ≥ 50%. 여기에 **3가지 2차 시그널 체크** 추가로 필터옵션/코드테이블 가드 ([validator.py:27-58](crawlers/analyzer/validator.py#L27-L58)):
  1. 제목 평균 길이 ≥ 8자 (필터코드는 2~6자)
  2. 2차 시그널(회사·날짜·위치·URL·급여·공고ID 등) 보유 아이템 ≥ 50%
  3. `{id,code,name,value,label}` 3개 이하 필드만 가진 아이템이 80%+ → "코드테이블" 로 거부

**2단계 중첩 item_path 자동 탐지**: 응답이 `data.partTimeJobList[i].jobAdList[j]` 같은 2중 중첩이면 외부 배열엔 제목이 없다. validator 가 이 상황을 탐지해 item_path 를 `[*]` 와일드카드 구문으로 자동 업그레이드 (`data.partTimeJobList[*].jobAdList`). `_traverse_path` 가 양쪽(validator + api_crawler)에서 `[*]` 를 지원.

#### Retry 루프 2종

**DOM retry** ([llm.py `retry_dom_selectors`](crawlers/analyzer/strategies/llm.py)): validator 가 DOM selector 실패로 거부 → LLM 에게 실패 사유와 기존 selectors 를 보내 **수정 제안** 요청 → 재검증. 최대 `VALIDATOR_RETRY_MAX` 회.

**API LLM Ranker retry** ([playwright_discovery.py `llm_rank_candidates`](crawlers/analyzer/strategies/playwright_discovery.py)): LLM 이 1위로 고른 API 가 validator 에 거부되면 **해당 idx 를 exclude 에 추가**하고 LLM 에게 "이 후보들 빼고 다시 골라라" 요청. pool 은 `result.config["_ranker_pool"]` (언더스코어 prefix 라 sites.json 저장 안 됨) 에 보존. 링커리어·알바몬 같은 GraphQL 필터옵션 API 오인식 케이스 대응.

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

### 5. HTTP 안정성 + 봇차단 우회

- 모든 HTTP 요청은 **기본 3회 재시도** + **점증적 대기(backoff)**
- 일시적 타임아웃, 네트워크 오류에 견고
- `main.py` 상단의 `HTTP_TIMEOUT`, `HTTP_MAX_RETRIES`, `HTTP_RETRY_BACKOFF`로 조정
- **curl_cffi 기반 TLS 지문 위장** — 내부적으로 `curl-impersonate` 를 쓰는 Python 바인딩. `impersonate="chrome131"` 로 실제 Chrome 의 JA3/HTTP2 프로파일을 흉내내서 사람인·잡플래닛 같은 JA3 지문 기반 봇판별 사이트를 뚫는다. 크롤러·분석기 전체가 [crawlers/http_client.py](crawlers/http_client.py) 의 `fetch()` 하나를 공유하므로 한 곳에서 일괄 적용됨.

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
│   ├── http_client.py            # curl_cffi (chrome131 impersonate) + 재시도 + 공통 헤더
│   ├── database.py               # SQLite (jobs / crawl_runs)
│   ├── healthcheck.py            # crawl_runs 이력 기반 이상 감지
│   └── slack_notifier.py         # 헬스체크 알림 + 배치 요약 알림
│
├── scripts/                      # 분류별 서브폴더
│   ├── batch/                    #  └ 배치 등록 (batch_add, batch_analyze)
│   ├── crawl/                    #  └ 크롤 실행 (crawl_full, crawl_one, run_crawl.bat)
│   ├── maintenance/              #  └ 유지보수 (cleanup_*, backfill, set_enabled)
│   ├── inspect/                  #  └ 조회 (list_sites, db_counts, sample_jobs)
│   ├── db/                       #  └ DB/엔트리 조작 (remove_site, wipe_source)
│   └── dev/                      #  └ 개발/테스트 (test_*, probe_proxy)
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

래퍼 스크립트로 [scripts/crawl/run_crawl.bat](scripts/crawl/run_crawl.bat) 를 제공한다 — cwd 를 맞추고, Python 절대경로로 `main.py crawl` 을 실행한 뒤, 결과를 `logs/scheduled-YYYYMMDD.log` 에 append 한다. 스케줄러에서는 이 bat 하나만 등록하면 됨.

#### PowerShell 로 한 번에 등록 (권장)

```powershell
$action   = New-ScheduledTaskAction -Execute "C:\Users\<사용자>\OneDrive\Documents\code\2604PJ_JobCrawler\scripts\crawl\run_crawl.bat"
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
2. **동작** 탭 → **프로그램 시작** → `scripts/crawl/run_crawl.bat` 의 절대경로
3. **트리거** 탭 → 매일 / 00:00
4. **설정** 탭 → "예약대로 시작하지 못한 경우 가능한 한 빨리 작업 시작" 체크, "작업을 중지하기까지 시간" = 1시간
5. **조건** 탭 → (노트북이면) "컴퓨터 AC 전원 사용 시에만 작업 시작" 해제
6. 저장 후 목록에서 우클릭 → **실행** 으로 수동 트리거해서 동작 확인. `logs/scheduled-<오늘>.log` 가 생성되면 성공.

#### 결과 확인

- Slack 알림 — 배치 요약이 바로 날아옴 (설정했다면)
- `logs/scheduled-YYYYMMDD.log` — 전체 stdout/stderr
- `python scripts/inspect/db_counts.py` — source 별 수집 건수 집계

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
- **requests** — HTTP 예외 타입 (curl_cffi 도 호환되게 던짐)
- **curl_cffi** — **Chrome TLS/JA3 지문 임퍼소네이트** — JA3 기반 봇판별 우회 (사람인·잡플래닛 등). `impersonate="chrome131"` 로 실제 Chrome 131 의 TLS 핸드셰이크 패턴까지 재현
- **BeautifulSoup4 + lxml** — HTML 파싱
- **Playwright** — SPA 사이트 네트워크 캡처 / 브라우저 자동화
- **OpenAI (GPT-4o-mini)** — analyzer 의 LLM 폴백 + 후보 API LLM Ranker
- **SQLite** — 데이터 저장 (Python 내장)
- **Streamlit + Plotly** — 대시보드 (`streamlit run dashboard.py`)
