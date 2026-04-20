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
  "site_id": "jobkorea-cambodia",
  "url": "https://www.jobkorea.co.kr/Search/?stext=...",
  "added_at": "2026-04-20T...",

  "site_type": "nuxt_ssr",              // 분류 (진단/통계용)

  "extraction_method": "embedded_json",  // 추출 로직 선택 (3종 중 1)
  "requires_render": true,               // Playwright 로 HTML 받을지

  "source": {
    // extraction_method 에 따라 스키마 다름.
    // dom:           {"selectors": {...}, "base_url": "..."}
    // api:           {"endpoint": "...", "method": "GET", "headers": {...}, "item_path": "data.result"}
    // embedded_json: {"script_selector": "#__NEXT_DATA__", "item_path": "props.pageProps.jobs"}
  },

  "field_mapping": {
    // 수집한 raw 아이템 → DB 스키마 매핑
    "title": "$.title",
    "company": "$.employer.company",
    "url": "https://...{$.id}",
    "posted_at": "$.pubdate"
  },

  "pagination": {
    "type": "url_param",           // url_param | api_param | dom_next_link | infinite_scroll | none
    "param": "page",
    "start": 1,
    "stop_when": "no_new_items"    // no_new_items | max_pages | empty_response
  },

  "validated": true,
  "validation_report": {
    "items_extracted": 53,
    "fields_matched": {"title": 53, "company": 50, "url": 53},
    "sample_titles": ["미얀마, 캄보디아 통번역상담사 모집", "..."]
  }
}
```

**키 결정의 이유:**

- `extraction_method` 는 크롤러 선택의 **유일한 축**. dispatcher 가 이것만 보고 분기.
- `requires_render` 는 추출 방법과 직교. DOM 이어도 렌더 필요할 수 있음 (잡코리아가 DOM 으로 풀린 경우).
- `source` 스키마를 method 별로 분리 — 하나로 통합하면 의미 없는 필드가 엔트리를 오염시킴.
- `field_mapping` 은 JSONPath 같은 단순 표현식. DOM 의 경우 `$.title` 대신 `{"selector": ".title", "attr": "text"}` 같은 객체 형태 허용 (확장).
- `validated=true` 없으면 sites.json 에 **절대 저장 안 함**. 이게 환각 방어선.

---

## 4. 파이프라인

### 4.1 Analyzer (등록 시점)

```
URL
 ↓
[Fetcher]
    HTTP fetch (requests) OR Playwright render → html, page_obj
 ↓
[Classifier]
    heuristic 시그니처 (__NUXT__, g5_bbs_url 등) + 필요 시 LLM 보정
    → site_type 결정, requires_render 힌트
 ↓
[Extractors]  (순차 시도, 첫 validator 통과 시 종료)
    1. APIDiscoveryExtractor       (Playwright XHR 캡처 + LLM 랭커)  ← 이미 구현됨
    2. EmbeddedJSONExtractor       (HTML <script> 에서 상태 JSON 찾기)
    3. DOMSelectorExtractor        (LLM 에게 HTML 주고 selectors 질문)
 ↓
[Validator]
    추출 방법별로 실제 돌려보고 ≥ N개 나오는지 확인
    통과 → 등록 / 실패 → 다음 Extractor
    모두 실패 → "수동 필요" 로 거부 (사람에게 정직한 실패)
 ↓
[Registrar]
    sites.json 에 엔트리 저장 (validated=true)
```

### 4.2 Crawler (수집 시점)

```
for entry in sites.json + REGISTERED_CRAWLS:
    dispatcher.pick(entry.extraction_method)
        ├─ "dom"           → DOMCrawler
        ├─ "api"           → APICrawler
        └─ "embedded_json" → EmbeddedJSONCrawler
    ↓
    각 크롤러 공통:
      - Fetcher (requires_render 보고 HTTP vs Playwright)
      - Paginator (pagination config 따라 루프)
      - field_mapping 적용해서 normalized item 생성
      - 아이템 건별 ItemValidator (필수 필드 non-empty 확인)
      - JobDatabase 에 upsert
    ↓
    RunReport (성공/실패/수집량) → healthcheck 연동
```

---

## 5. 모듈 레이아웃 (목표)

```
crawlers/
├── analyzer/
│   ├── models.py              # AnalysisResult, SiteType, ExtractionMethod enum
│   ├── fetcher.py             # 공용 HTTP + Playwright render
│   ├── classifier.py          # heuristic + LLM 하이브리드 (현 HeuristicStrategy 대체)
│   ├── validator.py           # ★ 핵심. DOM/API/EmbeddedJSON 각각 검증
│   ├── pipeline.py            # 오케스트레이션 (현 analyzer.py 대체)
│   └── extractors/
│       ├── base.py            # Extractor ABC
│       ├── api_discovery.py   # 현 playwright_discovery 이동
│       ├── embedded_json.py   # NEW
│       └── dom_selectors.py   # NEW
│
├── crawler/
│   ├── dispatcher.py          # extraction_method → 크롤러 인스턴스
│   ├── dom_crawler.py         # NEW (gnuboard/static_html/rendered_html 통합)
│   ├── api_crawler.py         # NEW (camhr 일반화)
│   └── embedded_crawler.py    # NEW
│
├── pagination.py              # NEW. Paginator ABC + 4가지 구현
├── field_mapping.py           # NEW. JSONPath / DOM selector 추출기
├── sites_registry.py          # validated=true 체크로 로직 바뀜
├── database.py                # (기존 유지)
└── hardcoded_crawls.py        # 최종 삭제 대상 (모든 사이트가 sites.json 로 이주)
```

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

### 6.3 Embedded JSON config 검증 (`validate_embedded_json(html, config)`)

통과 조건:
1. `source.script_selector` 로 `<script>` 찾기 성공
2. 내부 JSON 파싱 성공
3. `source.item_path` 로 꺼낸 값이 배열 ≥ **2** + 아이템이 텍스트성 필드 보유

### 6.4 공통: 수집 시점 ItemValidator

크롤 중 매 아이템 건별로:
- 필수 필드 (title, url) non-empty
- 이게 ≥ 80% 통과 못 하면 config 가 stale 된 것으로 간주 → RunReport 에 경고, healthcheck 로 전파

---

## 7. 현재 → 목표 매핑

| 현재 | 목표 | 비고 |
|---|---|---|
| `strategies/heuristic.py` | `classifier.py` | 역할 좁혀짐 (site_type 분류만) |
| `strategies/llm.py` | `extractors/dom_selectors.py` 로 재배치 | LLM 호출 패턴 유지, validator 가 앞단 |
| `strategies/playwright_discovery.py` | `extractors/api_discovery.py` 로 이동 | 기능 그대로 |
| (없음) | `extractors/embedded_json.py` | NEW — 잡코리아 같은 SSR 대응 |
| (없음) | `validator.py` | NEW — 환각 방어 |
| `gnuboard_crawler.py` | `crawler/dom_crawler.py` 의 preset | theme (nariya/fz) 는 selectors 사전으로 흡수 |
| `camhr_crawler.py` | `crawler/api_crawler.py` + sites.json 엔트리 | hardcoded_crawls 에서 이주 |
| `hardcoded_crawls.py` | 삭제 | 모든 사이트가 sites.json 로 |
| `SITE_TYPE_TO_CRAWLER` | `EXTRACTION_METHOD_TO_CRAWLER` | 축 변경 |

---

## 8. 케이스 시뮬레이션

설계가 4가지 실제 사이트를 커버하는지 점검.

### CamHR (API, 정상)
1. Classifier → spa_custom
2. APIDiscoveryExtractor → LLM 랭커가 `jobs/simple/page-query` 선택
3. Validator → 호출 시 `data.result` 배열 12개, title/employer 필드 존재 → 통과
4. Crawler: `api_crawler` + `pagination.type=api_param` + field_mapping

### 잡코리아 캄보디아 (SSR + DOM 후행)
1. Classifier → nuxt_ssr
2. APIDiscoveryExtractor → 후보 전부 메타데이터, LLM 이 -1 → **실패**
3. EmbeddedJSONExtractor → HTML 에서 `__NUXT__` 파싱, LLM 이 `state.result.list` 같은 경로 제안
4. Validator → state 파싱 성공, 배열 50+ 개, title 필드 존재 → 통과 (또는 여기서도 실패 시 DOMSelectorExtractor 로 폴백)
5. Crawler: `embedded_crawler` + pagination

### 인크루트 (공고 0건)
1. Classifier → spa_or_hybrid
2. APIDiscoveryExtractor → 후보 없음 (XHR 에 listing 없음)
3. EmbeddedJSONExtractor → state 없음
4. DOMSelectorExtractor → LLM 이 selectors 제안 → **Validator 에서 매칭 0개 → 실패**
5. 모두 실패 → "수동 필요" 거부. 정직한 실패.

### hanin (gnuboard)
1. Classifier → gnuboard (heuristic 강한 시그니처)
2. DOMSelectorExtractor → theme 사전에서 nariya 매칭 시 selectors 바로 사용, 없으면 LLM
3. Validator → list_rows 매칭, 제목 있음 → 통과
4. Crawler: `dom_crawler`

---

## 9. Phase 로드맵

### Phase 1: Validator + 범용 DOMCrawler
- `analyzer/validator.py` 구현 (3종 스펙 다 쓰되 DOM 만 실제 사용)
- `crawler/dom_crawler.py` 구현 (gnuboard_crawler 흡수)
- `SITE_TYPE_TO_CRAWLER` → `EXTRACTION_METHOD_TO_CRAWLER` 전환
- 기존 gnuboard 엔트리들 field_mapping + pagination config 마이그레이션
- 회귀 테스트: hanin/siemreap 동일 데이터 수집 확인

### Phase 2: EmbeddedJSON 추출 + Extractor 재배치
- `strategies/` → `extractors/` 이동 + base ABC 정리
- `extractors/embedded_json.py` + `crawler/embedded_crawler.py`
- 잡코리아 config 자동 생성 + 수집 검증

### Phase 3: API 경로 일반화
- `crawler/api_crawler.py` 구현
- camhr_crawler → sites.json 엔트리로 이주
- hardcoded_crawls.py 삭제

### Phase 4: 대량 발굴 + 실측
- 캄보디아 구인 사이트 50~100개 발굴
- `analyze` 배치 스크립트
- 성공률 / 실패 유형 분류 → 다음 개선 방향 결정

### Phase 5 (선택): 인터랙션 캡처
- 검색 버튼 클릭, 무한스크롤 유도 등 (`extractors/api_discovery.py` 확장)
- Cloudflare 우회는 이 단계에서

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
