# 자동 크롤링 아키텍처 설계 (step3/auto-crawling)

> **목적:** URL 1,500개가 들어와도 사이트별 코드 없이 수집되는 범용 파이프라인.
>
> 새 사이트가 추가되면 **config (JSON 한 덩어리)만** 생기고, 기존 크롤러 3종이 그 config를 먹고 수집한다. 사람이 개입하는 유일한 순간은 — 시스템이 "이 사이트는 자동화 못 함" 이라고 **정직하게 실패를 신고할 때**.

---

## 1. 문제 정의

현재 (`step2/api-camhr` 말미) 상태:

- 사이트 유형마다 전용 크롤러 파일 (`camhr_crawler.py`, `gnuboard_crawler.py`)
- 새 유형 = 새 파일 작성 (수동 작업 병목)
- `sites_registry.SITE_TYPE_TO_CRAWLER` 에 등록되지 않은 타입은 `analyze` 가 거부
- LLM 이 제안한 selectors 가 **검증 없이** 저장됨 → 프로덕션에서 0건 수집하고 나서야 환각 발견 (인크루트 케이스에서 확인)

1,500 사이트 스케일에서 이 구조는 붕괴한다.

---

## 2. 핵심 통찰: 추출 방법은 3개뿐

웹사이트 UI/프레임워크는 다양해도 **"데이터를 어디서 꺼내는가"** 로 보면 3가지로 귀결된다.

| 추출 방법 | 데이터 위치 | 대표 예시 | 필요 도구 |
|---|---|---|---|
| **DOM** | HTML DOM 요소 | gnuboard, 정적 HTML, SSR 렌더 후 | requests OR Playwright render |
| **API** | XHR/fetch JSON 응답 | CamHR, 순수 SPA | requests (+ Playwright 로 발견) |
| **Embedded JSON** | `<script>` 안 `__NUXT__` / `__NEXT_DATA__` / inline state | 잡코리아 같은 SSR Nuxt/Next | requests OR Playwright |

지금 코드의 구조적 결함: **"site_type (분류)" 에 "추출 방법" 이 얽혀있음.**
예: `gnuboard` 는 항상 DOM, `api_discovered` 는 항상 API 로 1:1 매핑됨.

실제론 같은 Nuxt 사이트도 —
- XHR 로 API 가 뜨면 **API** 로 추출
- XHR 이 없고 HTML 에 state 가 박혀있으면 **Embedded JSON** 으로 추출
- 둘 다 없으면 렌더 후 **DOM** 으로 추출

**→ 분류축과 추출축을 분리해야 한다.**

---

## 3. Config 스키마 (sites.json 엔트리)

```json
{
  "site_id": "hanin",
  "url": "http://www.hanin.or.kr/bbs/board.php?bo_table=Information",
  "added_at": "2026-04-17T...",

  "site_type": "gnuboard",              // 분류 (진단/통계용)

  "extraction_method": "dom",            // 추출 로직 선택 (3종 중 1)
  "requires_render": false,              // Playwright 로 렌더 필요한지

  "source": {
    // extraction_method 에 따라 스키마 다름. 현재 구현:
    //
    // dom:
    //   {"list_url", "list_params", "base_url", "selectors": {...}, "parse_mode",
    //    "skip_row_if_has_class": [...], "external_id_from_url_param"}
    //
    // embedded_json (requires_render=false):
    //   {"list_url", "list_params", "base_url", "script_selector", "item_path"}
    //
    // embedded_json (requires_render=true, Phase 2.5):
    //   {"list_url", "list_params", "base_url", "state_source", "item_path",
    //    "wait_for_ms"}   // state_source 예: "window.__NUXT__", "window.__NEXT_DATA__"
    //
    // api:  (Phase 3 미구현)
  },

  "pagination": {
    "type": "url_param",           // url_param | api_param | dom_next_link | infinite_scroll | none
    "param": "page",
    "start": 1
  },

  "validated": true,
  "validation_report": {
    "items_extracted": 14,
    "fields_matched": {"title": 14},
    "sample_titles": ["...", "...", "..."]
  }
}
```

**키 결정의 이유:**

- `extraction_method` 는 크롤러 선택의 **유일한 축**. dispatcher 가 이것만 보고 분기.
- `requires_render` 는 추출 방법과 직교. embedded_json 이어도 렌더 필요할 수 있음 (`__NUXT__` 팩토리 형태 — Phase 2.5).
- `source` 스키마를 method 별로 분리 — 하나로 통합하면 의미 없는 필드가 엔트리를 오염시킴.
- **렌더 경로의 `state_source`** 는 JavaScript expression (`window.__NUXT__` 등). crawler 가 `page.evaluate()` 로 평가해 state 추출.
- `validated=true` 없으면 sites.json 에 **절대 저장 안 함**. 이게 환각 방어선.
- `field_mapping` 은 현재 구현 안 됨 — 각 크롤러가 내장된 휴리스틱 키 리스트 (TITLE_KEYS, COMPANY_KEYS 등) 로 필드 자동 추출. 장기적으로 JSONPath 표현식 도입 예정.

---

## 4. 파이프라인

### 4.1 Analyzer (등록 시점) — 현재 구현

```
URL
 ↓
[HeuristicStrategy]
    HTTP fetch + 시그니처 매칭 (__NUXT__, __NEXT__, g5_bbs_url, generator meta 등)
    → site_type (gnuboard/static_html/spa_*) + confidence
    → SPA 판정이면 heuristic_type 기억해두고 다음 전략으로 체인
 ↓
[PlaywrightDiscoveryStrategy]  — heuristic_type ∈ SPA_TYPES 일 때만 실행
    headless Chromium 으로 XHR 캡처 → URL/페이로드 규칙 점수화
    LLM 랭커가 메타데이터/트래킹 걸러내고 진짜 공고 API 선택
    LLM 명시 거부 (-1) 시 → 규칙점수 폴백 금지, UNKNOWN 반환 (환각 방어)
    성공 시 site_type=api_discovered
 ↓
[EmbeddedJSONStrategy]  — heuristic_type ∈ SPA_TYPES + playwright 실패 시
    1차: HTML 에서 <script id="__NEXT_DATA__"> 찾아 innerText JSON 파싱
    2차 (Phase 2.5): use_playwright_render=True 면 페이지 렌더 후
         page.evaluate("() => window.__NEXT_DATA__ / __NUXT__ / __INITIAL_STATE__ /
                           __APOLLO_STATE__") 로 전역 state 추출
    state 내 배열 후보 수집 → LLM 이 공고 리스트 경로 선택
    LLM 명시 거부 ("") 시 → 휴리스틱 폴백 금지 (환각 방어)
    성공 시 site_type=embedded_json_discovered (+ requires_render 마킹)
 ↓
[LLMStrategy]  — heuristic 미확정 (static_html 등) 일 때만, SPA 엔 스킵
    HTML 주고 gnuboard/static_html/… 분류 + selectors 제안
 ↓
[cmd_add: Validator]
    extraction_method 별 validator 실제 돌려봄:
      - validate_dom_config(html, config)
      - validate_embedded_json_config(html, config, url=...)
        ← requires_render=True 면 내부에서 Playwright 렌더 후 검증
    통과 실패 → 저장 거부 (침묵 실패 방지)
 ↓
[Registrar]
    sites.json 에 엔트리 저장 (validated=true + validation_report)
```

### 4.2 Crawler (수집 시점)

```
for entry in REGISTERED_CRAWLS + sites.json:
    dispatcher.dispatch(entry)
      ├─ extraction_method == "dom"           → dom_crawler.crawl(...)
      ├─ extraction_method == "api"           → NotImplementedError (Phase 3)
      ├─ extraction_method == "embedded_json" → embedded_crawler.crawl(...)
      │     ├─ requires_render=False → requests fetch + <script> 파싱
      │     └─ requires_render=True  → 페이지별 Playwright 렌더 + page.evaluate
      └─ 레거시 crawler="camhr_crawler" / "gnuboard_crawler" → 해당 모듈
    ↓
    각 크롤러:
      - Paginator: pagination.type (url_param 만 현재 지원)
      - 아이템별 field 휴리스틱 (TITLE_KEYS, URL_KEYS, ...) 로 매핑
      - JobDatabase.upsert_job
      - CrawlRun 시작/종료 기록
    ↓
    cmd_crawl 종료 후 healthcheck.analyze() 자동 실행 → Slack 선택적 전송
```

---

## 5. 모듈 레이아웃

### 5.1 현재 구조 (Phase 2.5 시점)

```
crawlers/
├── analyzer/
│   ├── __init__.py
│   ├── analyzer.py              # SiteAnalyzer 오케스트레이터 (전략 순차 실행)
│   ├── models.py                # AnalysisResult, SiteType enum
│   ├── validator.py             # validate_dom_config / validate_embedded_json_config
│   │                            # (+ validate_api_config stub)
│   └── strategies/
│       ├── __init__.py
│       ├── base.py              # AnalysisStrategy ABC
│       ├── heuristic.py         # 시그니처 매칭 + site_type 분류
│       ├── playwright_discovery.py  # XHR 캡처 + LLM 랭커 (API 자동 발견)
│       ├── embedded_json.py     # HTML <script> 파싱 + Playwright 렌더 (Phase 2.5)
│       └── llm.py               # LLM 폴백 — gnuboard/static_html/spa selectors
│
├── dispatcher.py                # extraction_method 로 크롤러 모듈 선택
├── dom_crawler.py               # gnuboard/static_html 통합 DOM 크롤러
├── embedded_crawler.py          # embedded_json 크롤러 (HTML + 렌더 경로)
├── camhr_crawler.py             # 레거시 (Phase 3 에서 api_crawler 로 이주 예정)
├── gnuboard_crawler.py          # 레거시 (dom_crawler 완성 후 삭제 예정)
├── hardcoded_crawls.py          # REGISTERED_CRAWLS — analyzer 로 자동화 불가 사이트용
├── sites_registry.py            # sites.json I/O + analysis → config 변환 + can_register
├── database.py                  # JobDatabase
├── http_client.py               # requests + 재시도
├── healthcheck.py               # 수집 결과 분석
└── slack_notifier.py            # Slack 알림
```

### 5.2 목표 구조 (리팩터 완료 시)

- `analyzer/strategies/` → `analyzer/extractors/` 로 개명 (역할이 classifier vs extractor 로 분화되면)
- `crawler/` 서브디렉토리로 크롤러들 이동 + `api_crawler.py` 신규
- `pagination.py` + `field_mapping.py` 모듈로 로직 분리
- `hardcoded_crawls.py` 삭제 — 모든 사이트가 sites.json 로 이주

**현재 도달도**: analyzer 분리 ✓, validator 분리 ✓, dom/embedded 크롤러 ✓. 남은 건 api_crawler + 레거시 이주 + 구조적 분리.

---

## 6. Validator 규칙 (가장 중요)

환각 방어선. LLM 이 뭘 제안하든 **실제로 돌려봤을 때** 조건을 통과해야 `validated=true`.

### 6.1 DOM config 검증 (`validate_dom_config(html, config)`)

통과 조건:
1. `selectors.list_rows` 로 soup.select 시 ≥ **2개** 매칭
2. 각 row 안에서 `subject_link` 가 **1개 이상** 매칭되고 텍스트 non-empty
3. 텍스트가 네비게이션/UI 노이즈처럼 보이지 않음 (길이 5자 이상, "로그인"/"홈"/"검색" 같은 단독 키워드 아님)

리포트 내용: items_extracted, fields_matched (%), sample_titles (최대 3개)

### 6.2 API config 검증 (`validate_api_config(config)`)

통과 조건:
1. 실제 endpoint 호출 → HTTP 200 + JSON 파싱 성공
2. `source.item_path` (JSONPath) 로 꺼낸 값이 배열이고 길이 ≥ **2**
3. 배열 첫 아이템이 텍스트성 필드를 ≥ **1개** 가짐 (title/company/name 등 공고 필드 후보)

### 6.3 Embedded JSON config 검증 (`validate_embedded_json_config(html, config, url)`)

두 경로 분기:

**HTML 경로** (`requires_render=False`):
1. `source.script_selector` 로 `<script>` 찾기 성공
2. 내부 JSON 파싱 성공
3. `source.item_path` 로 꺼낸 값이 배열 ≥ **2** + 아이템이 `EMBEDDED_TITLE_KEYS`
   (title/jobTitle/postSubject/...) 중 하나를 비율 ≥ **50%** 로 보유

**렌더 경로** (`requires_render=True`, Phase 2.5):
1. Playwright 로 url 열고 `source.state_source` (예: `window.__NUXT__`) 를
   `page.evaluate()` 로 평가 → state 획득
2. state 가 dict/list 이어야 함
3. 이하 3번은 HTML 경로와 동일 (item_path + 제목성 필드)

+ 공통: 추출된 제목이 UI 노이즈 키워드 (로그인/홈/검색 등) 뿐이면 거부.

### 6.4 공통: 수집 시점 ItemValidator

크롤 중 매 아이템 건별로:
- 필수 필드 (title, url) non-empty
- 이게 ≥ 80% 통과 못 하면 config 가 stale 된 것으로 간주 → RunReport 에 경고, healthcheck 로 전파

---

## 7. 현재 → 목표 매핑

| 항목 | 현재 상태 | 비고 |
|---|---|---|
| `SITE_TYPE_TO_CRAWLER` → `EXTRACTION_METHOD_TO_CRAWLER` | ✓ 완료 (Phase 1) | dispatcher 가 method 기준 분기 |
| `validator.py` (DOM/EmbeddedJSON) | ✓ 완료 (Phase 1 + 2 + 2.5) | API 는 stub |
| `dom_crawler.py` | ✓ 완료 (Phase 1) | gnuboard/static_html 통합 |
| `embedded_crawler.py` | ✓ 완료 (Phase 2) + 렌더 경로 (Phase 2.5) | |
| `strategies/embedded_json.py` | ✓ 완료 (Phase 2) + 렌더 폴백 (Phase 2.5) | `__NUXT__` 팩토리 대응 |
| `strategies/playwright_discovery.py` LLM 명시 거부 존중 | ✓ 완료 (Phase 2.5) | 환각 방어 — 규칙점수 폴백 금지 |
| `strategies/llm.py` excerpt 개선 | ✓ 완료 (Phase 2.6) | script 제거 + 속성 절단 + dense-window |
| `strategies/heuristic.py` SPA 시그니처 엄격화 | ✓ 완료 (Phase 2.6) | 문자열 매칭 → 할당/id 속성 매칭 |
| `strategies/` → `extractors/` 디렉토리 개명 | 미완 | 이름 충돌 없고 영향 크지 않아 후순위 |
| `api_crawler.py` | 미완 (Phase 3) | camhr 일반화 대상 |
| `camhr_crawler.py` → sites.json 이주 | 미완 (Phase 3) | |
| `hardcoded_crawls.py` 삭제 | 미완 (Phase 3 이후) | 전체 이주 전까진 유지 |
| `field_mapping` JSONPath 표현식 | 미완 | 현재는 크롤러 내장 휴리스틱 키 리스트로 대체 |

---

## 8. 케이스 시뮬레이션 (실측 포함)

### hanin (gnuboard) — ✓ 정상 수집
1. Heuristic → `gnuboard` (시그니처 g5_bbs_url + bo_table 파라미터)
2. confidence=0.90, 바로 반환 — playwright/embedded/llm 스킵
3. theme=nariya 매칭 → selectors 자동 생성 (list_rows / subject_link)
4. validator: list_rows 14개, title 14/14 → 통과
5. crawler: `dom_crawler` + pagination.type=url_param

### siemreap (gnuboard, 캄보디아 게시판) — ✓ 정상 수집
hanin 과 동일 경로, 8건 수집.

### CamHR (API, 현재 하드코딩) — ⏳ Phase 3
현재는 `hardcoded_crawls.py` 의 레거시 어댑터로 수집 중. Phase 3 에서:
1. Heuristic → spa_custom
2. PlaywrightDiscoveryStrategy → LLM 랭커가 `jobs/simple/page-query` 선택
3. Validator (`validate_api_config`) → `data.result` 배열 확인
4. Crawler: 신규 `api_crawler`

### Wanted (Next.js SSR, 메타만 있음) — ✓ 정직한 실패
1. Heuristic → spa_next
2. PlaywrightDiscoveryStrategy → 후보 캡처되지만 메타데이터뿐, LLM=-1 → UNKNOWN
3. EmbeddedJSONStrategy → `#__NEXT_DATA__` 파싱 OK, 배열 후보 10개 중 LLM 판정
   "모두 카테고리/필터/광고" → **`""` 명시 거부**, 휴리스틱 폴백 금지
4. 등록 거부. 정직한 실패. (공고 리스트가 initial state 에 없고 user 상호작용 후 XHR 로만 로드)

### JobKorea `/Search/` (Next.js CSR) — ✗ 정직한 실패
1. Heuristic → `spa_next` (`/_next/` 시그니처 39회)
2. PlaywrightDiscoveryStrategy → 16개 XHR 캡처, 전부 `codes/benefit` 등 메타 API,
   LLM=-1 → UNKNOWN (환각 방어 적용 후 정상)
3. EmbeddedJSONStrategy HTML 경로 → `#__NEXT_DATA__` 없음
4. EmbeddedJSONStrategy 렌더 경로 → `window.__NEXT_DATA__` / `__NUXT__` /
   `__INITIAL_STATE__` / `__APOLLO_STATE__` **전부 없음** → UNKNOWN
5. LLMStrategy → SPA 로 판정됐으니 스킵 (HTML 이 빈 껍데기라 selectors 불가)
6. 등록 거부. CSR + custom XHR + 세션 — 표준 패턴 밖. `/recruit/joblist` 로 유도.

### JobKorea `/recruit/joblist` (SSR) — ✓ Phase 2.6 해결
실측: HTML 내 `tr.devloopArea` 64개로 **공고가 SSR 렌더**됨.
1. Heuristic → `static_html` (0.3 confidence — SPA 시그니처 0개)
2. LLMStrategy → `static_html` 0.9 confidence, 정확한 selectors:
   - `list_rows: tr.devloopArea`
   - `subject_link: td.tplTit a.link`
   - `author: td.tplCo a.link`
   - `date: span.time`

**Phase 2.6 개선점** (이걸 가능하게 한 것):
- `_build_llm_excerpt`: script/style/주석 제거 + 긴 속성값 절단 + 공백 축소 +
  반복 DOM 구간 검색 (tbody+5tr+anchor 우선, 그 외 같은 class 10회+anchor)
- MAX_HTML_CHARS 20k → 150k (정리 후 기준)
- Heuristic `__NUXT__` → `window.__NUXT__=` 정규식 할당 매칭으로 엄격화
- Heuristic `__NEXT_DATA__` → `id="__NEXT_DATA__"` 속성 매칭으로 엄격화

---

## 9. Phase 로드맵

### Phase 1: Validator + 범용 DOMCrawler — ✓ 완료
- `analyzer/validator.py` (DOM) + `dom_crawler.py` (gnuboard 흡수)
- `SITE_TYPE_TO_CRAWLER` → `EXTRACTION_METHOD_TO_CRAWLER` 전환
- 회귀 확인: hanin 14건, siemreap 8건

### Phase 2: EmbeddedJSON 추출 — ✓ 완료
- `strategies/embedded_json.py` (HTML `#__NEXT_DATA__` 파싱 + LLM 경로 선택)
- `embedded_crawler.py` (requires_render=False 경로)
- `validator.validate_embedded_json_config`
- LLM 명시 거부 sentinel → 휴리스틱 폴백 금지 (환각 방어)

### Phase 2.5: Playwright 렌더 경로 — ✓ 완료
- EmbeddedJSONStrategy 에 렌더 폴백 (`window.__NUXT__` / `__NEXT_DATA__` /
  `__INITIAL_STATE__` / `__APOLLO_STATE__` evaluate)
- embedded_crawler 에 페이지별 렌더 경로
- validator 렌더 경로 지원 (url 인자)
- PlaywrightDiscoveryStrategy 도 LLM 명시 거부 존중 (환각 방어 통일)
- 실측: JobKorea 는 표준 state 패턴 밖 → 정직한 실패. 다른 Nuxt 팩토리 /
  CSR 사이트에는 유효.

### Phase 2.6: LLM excerpt 개선 + Heuristic SPA 엄격화 — ✓ 완료
- `_build_llm_excerpt`: 대형 사이트(JobKorea 급)에서 실제 목록 DOM 이 HTML offset
  100k+ 뒤에 있어 기존 20k excerpt 로 LLM 이 selectors 환각하던 문제 해결.
  - script/style/noscript/HTML 주석 제거
  - 긴 속성값(>200자, 보통 inline JSON data-*) 절단
  - 공백 축소
  - 반복 DOM 구간 검색 (tbody+tr+anchor 우선) 후 head+dense-window 조합
  - MAX_HTML_CHARS 20k → 150k
- Heuristic `__NUXT__` 매칭을 `window.__NUXT__=` 할당 정규식으로 엄격화
  (문자열/주석 오탐 방지)
- Heuristic `__NEXT_DATA__` 매칭을 `id="__NEXT_DATA__"` 속성으로 엄격화
- 실측: JobKorea `/recruit/joblist` → `tr.devloopArea` + `td.tplTit a.link` 등
  정확한 selectors 생성. confidence 0.9.

### Phase 3: API 경로 일반화 — 미완
- `api_crawler.py` 구현 (camhr 일반화)
- `validate_api_config` 실구현
- camhr_crawler → sites.json 엔트리로 이주
- hardcoded_crawls.py 축소/삭제

### Phase 4: 대량 발굴 + 실측 — 미완
- 캄보디아 구인 사이트 50~100개 발굴
- `analyze` 배치 스크립트
- 성공률 / 실패 유형 분류

### Phase 5 (선택): 인터랙션 캡처 — 미완
- 검색 버튼 클릭, 무한스크롤 유도 등
- Cloudflare 우회

### 구조 정리 (병렬) — 미완
- `strategies/` → `extractors/` 개명
- `crawler/` 서브디렉토리로 크롤러들 묶기
- `pagination.py` / `field_mapping.py` 모듈 분리

---

## 10. 열린 질문

구현 들어가기 전 / 들어가면서 결정해야 할 것들:

- **field_mapping 표현식 스펙**
  JSONPath 라이브러리 쓸지 (jsonpath-ng), 자체 단순 구문 쓸지. 자체 구문이 빠르게 출발 가능.

- **pagination.stop_when 알고리즘**
  `no_new_items` 를 어떻게 판정? 이전 페이지와 아이템 ID set 비교? N페이지 연속 0건?

- **Validator 임계치 튜닝**
  지금 "≥ 2 개" 라는 숫자는 러프. 실측 데이터 쌓이면 조정.

- **extractor 간 공유 캐시**
  fetcher 가 받은 HTML 을 extractor 들이 재사용 (지금처럼 각자 fetch 하지 않게).

- **크롤 시점 vs 등록 시점 재검증 주기**
  config 가 stale 되는 경우 (사이트 리뉴얼) 어떻게 자동 감지/재분석 트리거?
