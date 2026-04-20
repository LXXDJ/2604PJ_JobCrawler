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
    // api:  (Phase 3)
    //   {"api_endpoint", "method" (GET|POST), "base_url", "request_headers",
    //    "list_params", "item_path", "total_path" (선택),
    //    "link_template" (선택), "detail_endpoint_template" (선택),
    //    "detail_method" (선택, 기본 GET), "detail_content_path" (선택)}
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
      ├─ extraction_method == "api"           → api_crawler.crawl(...)
      │     ├─ GET/POST 으로 api_endpoint 호출 + item_path 로 배열 도달
      │     ├─ total_path 있으면 총 페이지 수 자동 인식
      │     ├─ link_template / detail_endpoint_template 있으면 상세 본문도 수집
      │     └─ 신규만 detail 호출 (재확인 시 부하 최소화)
      ├─ extraction_method == "embedded_json" → embedded_crawler.crawl(...)
      │     ├─ requires_render=False → requests fetch + <script> 파싱
      │     └─ requires_render=True  → 페이지별 Playwright 렌더 + page.evaluate
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
├── api_crawler.py               # API 기반 크롤러 (Phase 3 — camhr 이주 완료)
├── hardcoded_crawls.py          # REGISTERED_CRAWLS — 현재 비어있음 (camhr 이주 후)
├── sites_registry.py            # sites.json I/O + analysis → config 변환 + can_register
├── database.py                  # JobDatabase
├── http_client.py               # requests + 재시도
├── healthcheck.py               # 수집 결과 분석
└── slack_notifier.py            # Slack 알림
```

### 5.2 목표 구조 (리팩터 완료 시)

- `analyzer/strategies/` → `analyzer/extractors/` 로 개명: **유보**. 원래 전제였던
  "classifier 와 extractor 의 역할 분화" 가 아직 없음 — 지금 개명하면 diff 만 크고
  blame 추적성 손실. 실제 분화 필요 시점에 클래스 이름까지 함께 개명 예정.
- `crawler/` 서브디렉토리로 크롤러들 이동: 미완 (부가적 개선)
- `pagination.py` + `field_mapping.py` 모듈로 로직 분리: 미완
- `hardcoded_crawls.py` 삭제: 현재 비어있어 파일 자체 제거 후보. 다만 특수 사이트
  대비용 "예비 Hook" 으로 남겨둠 — 완전 삭제는 사용 사례 확인 후.

**현재 도달도**: analyzer 분리 ✓, validator 분리 ✓, dom/embedded/api 크롤러 ✓,
camhr 이주 ✓, gnuboard_crawler 레거시 제거 ✓.

---

## 6. Validator 규칙 (가장 중요)

환각 방어선. LLM 이 뭘 제안하든 **실제로 돌려봤을 때** 조건을 통과해야 `validated=true`.

### 6.1 DOM config 검증 (`validate_dom_config(html, config)`)

통과 조건:
1. `selectors.list_rows` 로 soup.select 시 ≥ **2개** 매칭
2. 각 row 안에서 `subject_link` 가 **1개 이상** 매칭되고 텍스트 non-empty
3. 텍스트가 네비게이션/UI 노이즈처럼 보이지 않음 (길이 5자 이상, "로그인"/"홈"/"검색" 같은 단독 키워드 아님)

리포트 내용: items_extracted, fields_matched (%), sample_titles (최대 3개)

### 6.2 API config 검증 (`validate_api_config(config)`) — Phase 3 실구현

통과 조건:
1. `source.api_endpoint` 에 GET/POST 호출 → HTTP 200 + JSON 파싱 성공
2. `source.item_path` (dot-notation) 으로 꺼낸 값이 배열이고 길이 ≥ **2**
3. 배열 아이템의 ≥ **50%** 가 제목성 필드 (`title`/`jobTitle`/`postSubject`/...)를
   가짐 + 추출된 제목이 UI 노이즈만은 아님

구현 세부:
- 캡처 헤더 중 `:authority` 같은 HTTP/2 의사헤더와 `cookie`/`host`/`content-length`
  는 자동 제거 (requests 가 재전송하면 방해됨).
- GET 은 `list_params` → 쿼리스트링. POST 는 `list_params` → JSON body.
- 실패 사유는 ValidationReport.reason 에 기록 → sites.json 에 그대로 박힘.

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
| `retry_dom_selectors` validator-피드백 retry | ✓ 완료 (Phase 2.7) | cmd_add 에서 DOM 검증 실패 시 LLM 재시도 (최대 2회) |
| `extract_site_id` 서브도메인 접두어 스킵 | ✓ 완료 (Phase 2.7) | job/api/recruit 등 의미 없는 prefix 자동 스킵 |
| `strategies/` → `extractors/` 디렉토리 개명 | 미완 | 이름 충돌 없고 영향 크지 않아 후순위 |
| `api_crawler.py` | ✓ 완료 (Phase 3) | camhr 일반화 — GET/POST + item_path + detail 지원 |
| `validate_api_config` | ✓ 완료 (Phase 3) | stub → 실제 API 호출 + 배열 검증 |
| `camhr_crawler.py` → sites.json 이주 | ✓ 완료 (Phase 3) | 모듈 삭제, REGISTERED_CRAWLS 에서 camhr 제거 |
| `hardcoded_crawls.py` 비움 | ✓ 완료 (Phase 3) | 파일은 유지 (향후 특수 케이스 대비) |
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

### CamHR (API) — ✓ Phase 3 이주 완료
1. Heuristic → spa_nuxt (`/_nuxt/` + `data-n-head`)
2. PlaywrightDiscoveryStrategy → 11개 JSON XHR 캡처, LLM 랭커가
   `https://api.camhr.com/v1.0.0/jobs/simple/page-query` 선택
3. `sites_registry._api_analysis_to_source` 가 URL 쿼리를 list_params 로 쪼개고
   페이지 파라미터 (`page`) 를 pagination 쪽으로 분리. `response_shape.nested_array_path`
   에서 item_path = `data.result` 자동 추출.
4. `validate_api_config` → 엔드포인트 실호출, 12건 `title` 매칭 확인 → 통과.
5. 등록 후 사이트 운영자가 한 번 polish — Playwright 가 캡처한 `urgent=true&isFirst=true`
   필터 제거, `size=50` 로 확장, `total_path`/`link_template`/`detail_endpoint_template`
   수작업 추가. 이 polish 단계는 **자동 발견이 완벽할 수 없다는 한계의 보정**.
6. Crawler: `api_crawler` — 50건 수집 성공, 제목/회사/지역/본문 모두 채워짐.

**Phase 3 후 구조 변경**:
- `hardcoded_crawls.py` 에서 camhr 제거 → `REGISTERED_CRAWLS = []` 로 비움
- `crawlers/camhr_crawler.py` 파일 삭제
- `dispatcher` 에서 `camhr_crawler` 레거시 분기 제거

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

### Phase 2.7: Validator-피드백 retry 루프 + site_id 보강 — ✓ 완료

**배경**: 한국 주요 구인 사이트 8개 (원티드/캐치/사람인/인크루트/알바천국/알바몬/잡플래닛/고용24)
실측 후 failure mode 분류:
- **Mode A (LLM 셀렉터 환각)**: 3건 — 사람인, 인크루트, 고용24
- **Mode B (CSR 빈 껍데기)**: 1건 — 알바천국
- **Mode D (HTTP 404/403)**: 3건 — 캐치, 알바몬, 잡플래닛
- 성공: 1건 — 원티드 (api_discovered)

Mode A 가 37.5% 를 차지해 "validator 가 reject 한 dom config 를 LLM 에 피드백 주고 재시도"
가 의미 있다고 판단, Phase 2.7 구축.

**구현**:
- `crawlers/analyzer/strategies/llm.py::retry_dom_selectors(html, failed_selectors, failure_reason, …)`
  LLM 에 "이전 selectors + validator 실패 사유 + HTML excerpt" 를 던져 수정 제안받음.
  이전과 완전 동일한 selectors 반환 시 즉시 포기 (같은 환각 반복 방지).
- `main.py cmd_add` 의 DOM 경로에서 validator 실패 시 최대 `VALIDATOR_RETRY_MAX=2` 회 retry.
  성공 시 `validation_report.retry_history` 에 각 attempt 기록 첨부.
- `sites_registry.extract_site_id` 보강 — 의미 없는 서브도메인 접두어(`job`, `api`, `recruit`,
  `career`, `shop`, `mobile`, `www` 등) 자동 스킵. `job.incruit.com` → `incruit`.

**실측 결과 (Mode A 3건 재테스트)**:
- 사람인: retry 1회 — LLM 이 동일 selectors 반환 → 조기 포기 → 실패
- 인크루트: 초기부터 통과 (60/60 매칭) — retry 미발동, 성공
- 고용24: retry 1회 — LLM 이 동일 selectors 반환 → 조기 포기 → 실패

**한계와 해석**:
- 실제 retry 로 구제된 사이트 = **0건**. 인크루트는 초기 성공이라 Phase 2.7 공헌 아님.
- 사람인·고용24 의 진짜 문제는 "initial HTML 에 공고 DOM 자체가 없음"
  (XHR/form POST 이후 채워짐) — LLM 이 피드백을 받아도 답 없음. retry 는 환각이 아닌
  **허공을 가리키는 실수** 에 무력.
- 그럼에도 retry 는 **안전망** 으로 유지 가치 있음: LLM 비결정성 (같은 입력에 다른 답)
  대응, 드물게 나올 "selector 미세 오탈자" 구제.
- **다음 단계**: retry 에 Playwright 렌더된 HTML 을 넘기면 사람인/고용24 같은 케이스도
  구제 가능 — 별도 Phase 로 분리 (`Phase 2.8` 후보).

### Phase 3: API 경로 일반화 — ✓ 완료
- `api_crawler.py` 신설 — GET/POST + item_path + pagination(api_param) + 선택적 detail 조회
  - camhr_crawler 의 고정 로직을 일반화: base URL/헤더/파라미터/경로 전부 config 에서 읽음
  - `link_template`, `detail_endpoint_template`, `detail_content_path` 로 상세 본문 조립
  - 필드 휴리스틱 (TITLE/COMPANY/URL/...) 은 embedded_crawler 와 같은 키 후보 목록
  - dict 값 fallback 키에 `"company"` 포함 → camhr 처럼 `employer: {company: ...}` 중첩 대응
- `validate_api_config` 실구현 — stub 제거, 실제 API 호출 + item_path 배열 확인 + 제목성 필드 50%
- `_api_analysis_to_source` 추가 — PlaywrightDiscovery 결과를 sites.json 신 스키마로 변환
  - api_endpoint 의 쿼리스트링 → list_params 분리
  - page 파라미터 후보 자동 검출 → pagination.param 으로 이동
  - response_shape.nested_array_path / array_field → item_path 자동 추출
- `sites_registry.can_register` 에서 `api_discovered` 거부 제거 → 등록 허용
- `dispatcher` 에 api 라우팅 추가 + 레거시 `camhr_crawler` 분기 제거
- camhr 이주 완료: hardcoded_crawls.py 에서 제거, sites.json 에 등록
- `crawlers/camhr_crawler.py` 파일 삭제

**남은 한계 (운영자가 polish 해야 하는 것)**:
- Playwright 가 캡처하는 엔드포인트는 "시작 페이지가 그 순간 호출한 것" 이라 필터가 섞일 수 있음
  (camhr 홈이 `urgent=true` 로 요청했듯). 필요하면 사람이 sites.json 에서 필터 제거.
- `total_path`, `link_template`, `detail_*` 은 response 구조를 보고 사람이 넣어야 함
  (auto-discovery 가 상세 API 는 캡처 안 함 — 상세 페이지에서만 호출되기 때문).

### Phase 4: 대량 발굴 + 실측 — 미완
- 캄보디아 구인 사이트 50~100개 발굴
- `analyze` 배치 스크립트
- 성공률 / 실패 유형 분류

### Phase 2.8 (후보): Playwright 렌더 HTML 을 retry 에 공급 — 미완
- Phase 2.7 의 retry 가 "HTML 에 공고 DOM 없음" 케이스 (사람인/고용24) 에 무력
- `cmd_add` 에서 `VALIDATOR_RETRY_USE_RENDER=True` 이면 2회차 retry 는 Playwright 로
  렌더한 HTML 을 LLM 에 보냄
- 비용: 등록 시점 브라우저 1회 → 운영 중은 영향 없음

### Phase 5 (선택): 인터랙션 캡처 — 미완
- 검색 버튼 클릭, 무한스크롤 유도 등
- Cloudflare 우회

### 구조 정리 (병렬) — 부분 완료
- ✓ gnuboard_crawler.py 레거시 제거 + dispatcher 분기 삭제 (Phase 3 후속)
- ⏸ `strategies/` → `extractors/` 개명 — 유보 (classifier/extractor 역할 분화 필요 시 진행)
- 미완: `crawler/` 서브디렉토리로 크롤러들 묶기
- 미완: `pagination.py` / `field_mapping.py` 모듈 분리

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
