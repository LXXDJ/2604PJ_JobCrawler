# JobCrawler 프로젝트 구조 설명

## 1. 큰 그림 — 두 단계로 나뉘어 있음

```
[1단계: 등록] 홈 URL 한 줄 → 그 사이트의 "공고 리스트 페이지" 찾기 → DB 저장
[2단계: 배치] 30분마다 등록된 모든 사이트를 순회 → 새 공고만 DB 적재
```

핵심 아이디어: **사람은 홈 URL만 주고**, 등록할 때 한 번 고생해서 "이 메뉴가 채용 공고 리스트다"를 자동으로 찾아둔 다음, 그 다음부터는 **그 URL만 무조건 긁어옴**.

---

## 2. 폴더별 역할

```
crawlers/
├── registration/   ← 1단계: 홈 → 채용 메뉴 자동 탐색·등록
├── batch/          ← 2단계: 등록된 URL 30분마다 크롤링
├── extractors/     ← HTML/JSON에서 공고 행/ID 뽑아내는 파서
├── fetchers/       ← 실제 HTTP 요청 (3가지 방식)
└── infra/          ← SQLite DB (sites / jobs / crawl_runs)

scripts/
├── ops/            ← 실제 운영 명령 (등록·배치·스케줄)
├── db/             ← DB 관리 (조회·삭제·이름변경)
└── debug/          ← 진단 (한 사이트가 왜 등록 안 되는지 까보기)

dashboard/          ← Streamlit 대시보드 (수집된 공고 보기)
data/crawler.db     ← SQLite 파일 (모든 데이터 여기)
logs/               ← 배치 실행 로그
```

---

## 3. 1단계 — 등록 파이프라인 (`crawlers/registration/`)

홈 URL을 주면 5단계로 처리:

| 단계 | 파일 | 하는 일 |
|---|---|---|
| ① discover | [menu_discovery.py](crawlers/registration/menu_discovery.py) | 홈에서 `<a>` 태그 다 긁어 "채용/공고/모집" 키워드로 점수 매김 |
| ② classify | [menu_classifier.py](crawlers/registration/menu_classifier.py) | LLM(gpt-4o-mini)에게 "이 링크 채용 리스트 맞아?" 분류시킴 |
| ③ validate | [menu_validator.py](crawlers/registration/menu_validator.py) | 그 URL 진짜 fetch해서 행 ≥2, 채용 키워드 비율 ≥30% 등 임계 검사 |
| ④ dedupe | [menu_dedupe.py](crawlers/registration/menu_dedupe.py) | 같은 리스트가 여러 메뉴로 노출되면 합치기 |
| ⑤ save | [register.py](crawlers/registration/register.py) | `sites` 테이블에 `sources[]` 로 저장 |

**fetcher fallback 순서** (정적이 안 되면 차례로 다음 단계):
1. `static` — `curl_cffi` 로 단순 GET
2. `dynamic` — Playwright 로 실제 브라우저 (JS 렌더링)
3. `xhr_html` — 브라우저가 쏘는 AJAX 응답을 가로채서 list HTML만 캡처
4. `api` — JSON API면 스키마 자동 학습

---

## 4. 2단계 — 배치 파이프라인 (`crawlers/batch/`)

[runner.py](crawlers/batch/runner.py) 의 흐름 (단순화):

```python
for site in active_sites:
    seen_ids = DB에서 이 사이트의 모든 external_id 로드  # 이전 배치까지 본 공고
    for source in site.sources:
        rows = crawl_list(source.url, already_seen_ids=seen_ids)
        for row in rows:
            insert_job(...)   # 새 공고면 DB에 raw 적재
```

**중요한 dedup 로직** ([list_crawler.py](crawlers/batch/list_crawler.py)):
- **cross-batch**: DB에 이미 있는 ID면 skip → 증분 수집
- **cross-page**: 이번 페이지 ID들이 이전 페이지 subset이면 break → "page 무시 사이트" 무한루프 차단
- **page 1에 새 ID 0개** → 즉시 break → 신규 없으면 빠르게 종료

---

## 5. 데이터베이스 ([crawlers/infra/db.py](crawlers/infra/db.py))

3개 테이블만:
- **sites** — 등록된 사이트 (홈 URL, 이름, status, sources JSON)
- **jobs** — 수집된 공고 (site_id, external_id, url, title, raw)
  - `UNIQUE 제약 없음` → 같은 공고가 sticky로 여러 번 박스에 노출되면 그만큼 row 적재
- **crawl_runs** — 배치 1회마다 1 row (성공/실패, 신규 N건, 시간)

---

## 6. 자동화 ([scripts/ops/install_schtask.py](scripts/ops/install_schtask.py))

```
Windows 작업 스케줄러 (30분 간격)
  ↓ 호출
run_batch_silent.vbs (CMD 창 안 뜨게 SW_HIDE)
  ↓ 호출
run_batch_hourly.bat
  ↓ 호출
python -m scripts.ops.run_batch --no-detail
  ↓ 결과
logs/hourly_batch.log + Slack 알림
```

지금 열려 있는 `install_schtask.py` 가 바로 이 스케줄을 등록하는 명령어 파일.

---

## 7. 평소 자주 쓰는 명령

```bash
# 사이트 1개 등록
python -m scripts.ops.register https://www.example.com

# 수동 배치 (라이브 출력 보면서)
python -m scripts.ops.run_batch
python -m scripts.ops.run_batch --site worldjob   # 특정 사이트만

# 상태 확인
python -m scripts.db.counts          # 사이트별 공고 누적 건수
python -m scripts.db.recent_runs     # 최근 배치 결과

# 대시보드
streamlit run dashboard/app.py

# 사이트가 등록 실패할 때 까보기
python -m scripts.debug.dry_register <url>
python -m scripts.debug.validate <url>
```

---

## 흐름 요약 한 줄

> **등록 = "이 사이트의 채용 리스트 URL이 어디냐" 한 번 찾기**, **배치 = 그 URL을 30분마다 쳐서 새 ID만 골라 DB에 쌓기.**

---

## 8. 크롤링 단계별 정리 + 사이트 매핑

### 등록 시점 fallback 단계 (validator)

정적이 안 되면 차례로 다음 단계로 내려감.

| 단계 | 이름 | 방식 | 비용 | 배치 시점 fetcher |
|---|---|---|---|---|
| **1단계** | `static` | curl_cffi 로 HTTP GET → HTML 파싱 | 매우 쌈 (수십~수백 ms) | `static` |
| **2단계** | `dynamic` | Playwright 브라우저로 JS 렌더링까지 | 비쌈 (2~10초/페이지, 수백 MB RAM) | `dynamic` |
| **3단계** | `xhr_html` | Playwright 로 한 번 띄워서 AJAX 응답(HTML fragment) endpoint 알아낸 뒤, 그 endpoint URL 을 source 로 갈아끼움 | 등록 1회만 비쌈, **배치는 static 만큼 쌈** | `static` (URL 만 재작성됨) |
| **4단계** | `api` | Playwright XHR 캡처에서 JSON 응답 발견 → schema 자동 학습 (id/title 필드 매칭) | 등록 1회만 비쌈, **배치는 가벼운 JSON GET 페이지네이션** | `api` + `api_schema` |

핵심: **3·4단계는 등록할 때만 비싸고 배치는 1단계 수준으로 가벼움.** 2단계가 진짜 비싼 케이스 (배치마다 매번 브라우저).

---

### 현재 등록된 6개 사이트 매핑

| site_id | 단계 | 이유 |
|---|---|---|
| **hanin** | **1단계** static | 평범한 PHP 게시판 (`bbs/board.php`), HTML 그대로 list 들어있음 |
| **siemreap** | **1단계** static | 하닌과 같은 게시판 구조 (`?page=N` 무시 사이트지만 fetch 자체는 static) |
| **hrdkorea** | **1단계** static | JSP `jobRecruit.do` 에 list HTML 그대로 — `currentPage=N` 페이지네이션만 학습 |
| **worldjob** | **3단계** xhr_html → static 으로 저장 | 메인 페이지는 SPA 라 정적 fetch 가 빈 shell. Playwright 로 띄워서 `getEpmtList.do` 라는 AJAX endpoint 가 list HTML 만 따로 반환하는 걸 발견 → 그 URL 을 source 로 저장하고 fetcher='static' 으로 둠 |
| **camhr** | **4단계** api | XHR 응답이 JSON. `/a/job` endpoint + id/title 필드 자동 매칭. 배치는 JSON 페이지네이션으로 1,711건 수집 |
| **cambojob** | **2단계** dynamic | anti-scraping 대응 (path-segment 페이지네이션, Referer 검사, 세션 쿠키 필요). static 으로는 차단당해서 배치마다 Playwright 로 가야 함 |

---

### 비용 분포 정리

```
1단계 (static)         hanin, siemreap, hrdkorea  ← 가장 가벼움
3단계 (→ static 저장)  worldjob                   ← 등록만 비쌌고 배치는 1단계급
4단계 (api)            camhr                      ← 배치는 가벼운 JSON GET
2단계 (dynamic)        cambojob                   ← 배치마다 매번 Playwright (비쌈)
```

**관찰**: 6개 사이트 중 5개가 결국 가벼운 fetcher 로 안착. 2단계(dynamic)에 머물러 있는 cambojob 만 배치 비용이 큼 — anti-scraping 때문에 어쩔 수 없는 케이스.
