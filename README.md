# JobCrawler

채용 사이트를 자동 등록 + 30분 단위 배치로 크롤링하는 파이프라인.
사람 개입을 최대한 배제하고 홈 URL 한 줄만 주면 사이트의 모든 공고를 모아온다.

---

## 흐름 한 줄 요약

```
[등록] 홈 URL → 메뉴 탐색 → LLM 분류 → 검증 → dedupe → sites.sources[] 저장
[배치] sources[] 순회 → 페이지네이션 따라 list 수집 → raw rows 적재 (cross-batch 증분)
```

---

## 디렉토리

| path | 역할 |
|---|---|
| [crawlers/registration/](crawlers/registration/) | 홈 → 사이트 등록 (discover/classify/validate/dedupe/register) |
| [crawlers/batch/](crawlers/batch/) | 등록된 source URL 무조건 크롤링 (runner/list_crawler/detail_crawler) |
| [crawlers/extractors/](crawlers/extractors/) | HTML → list/detail/external_id, API schema 자동 학습 |
| [crawlers/fetchers/](crawlers/fetchers/) | static (curl_cffi), dynamic (Playwright + XHR JSON/HTML 캡처), api |
| [crawlers/infra/](crawlers/infra/) | SQLite 스키마 + repos |
| [scripts/ops/](scripts/ops/) · [scripts/db/](scripts/db/) · [scripts/debug/](scripts/debug/) | CLI 진입점 — 운영 / DB 관리 / 진단 (아래 *주요 CLI* 참고) |
| [dashboard/](dashboard/) | Streamlit 대시보드 |
| data/crawler.db | SQLite (sites / jobs / crawl_runs) |

---

## DB 스키마 ([crawlers/infra/db.py](crawlers/infra/db.py))

- **sites** — `id (site_id)`, `home_url`, `name`, `status` (`pending|active|paused|dead`), `sources` (JSON), `consecutive_failures`, …
- **jobs** — `id`, `site_id` (FK), `external_id`, `url`, `title`, `raw`, `first_seen_at`, `last_seen_at`.
  **UNIQUE 제약 없음** — 같은 (site_id, external_id) 의 multiple row 허용 (sticky/promoted occurrence 별로 별개 row 적재).
- **crawl_runs** — 매 배치 1 row. `result`, `jobs_added`, `rows_seen`, `error`. SQLite `datetime('now')` 는 UTC 저장 — dashboard 에서 KST 변환.

DB 마이그레이션 (UNIQUE 제거) 은 `init_db()` 시 자동 적용. 기존 데이터 보존.

---

## 등록 파이프라인

각 사이트별 최초 등록 시, 한번만 실행. 등록된 URL 은 배치 단계에서 더이상 판별 X — 그냥 무조건 크롤링.

| 단계 | 모듈 | 핵심 |
|---|---|---|
| 1. discover | [menu_discovery.py](crawlers/registration/menu_discovery.py) | 홈 fetch → `<a>` 키워드 스코어링. SPA 면 dynamic fallback |
| 2. classify | [menu_classifier.py](crawlers/registration/menu_classifier.py) | LLM (gpt-4o-mini) 으로 `full / filtered / personal / unknown` 분류. 짧은 라벨 + 강 키워드면 unknown→filtered override |
| 3. validate | [menu_validator.py](crawlers/registration/menu_validator.py) | 임계 통과 시 list 인정. static→dynamic→XHR HTML→API schema 순서로 fallback |
| 4. dedupe | [menu_dedupe.py](crawlers/registration/menu_dedupe.py) | 중복 메뉴 흡수 (sample_links 의 external_id 기준 비교) |
| 5. save | [register.py](crawlers/registration/register.py) → upsert_site | sources[] 저장. 검증 0 통과면 `pending` |

### 검증 임계
- `MIN_ROWS = 2`, `MIN_SUBJ_RATIO = 0.5` (행마다 detail anchor 비율)
- `MIN_JOB_TITLE_RATIO = 0.3` 또는 `MIN_JOB_TITLE_COUNT = 2` — 채용 키워드. row 의 `title` + 인접 td/cell `row_text` 둘 다 검사
- `MAX_UNIQUE_PATH_RATIO = 0.5` — detail URL 들이 모두 다른 path 면 list 가 아닌 메뉴
- `MAX_EVENT_RATIO = 0.5` — 설명회/세미나/강의 같은 이벤트 키워드 차단
- `<nav>/<aside>/<header>/<footer>` 안에 있는 컨테이너는 후보에서 제외
- 모든 후보 컨테이너 중 임계 통과하는 것 중 가장 큰 것 채택 (sidebar 84-row 학교목록 + 본문 10-row 채용 list 공존 케이스 대응)

### dynamic fallback 전략 (validator)
정적 fetch 가 임계 미달 시 우선순위:
1. **Playwright dynamic fetch** + `capture_api=True` — XHR 응답 캡처 (JSON + HTML 둘 다, GET + POST 둘 다)
2. **JSON API schema 자동 학습** (camhr 케이스) — `detect_schema()` 가 가장 큰 list-of-dict 응답 + id/title 후보 필드 매칭. 발견 시 source = `{fetcher: "api", api_schema: {...}}`
3. **XHR HTML fragment endpoint 채택** (worldjob 케이스) — AJAX 가 list HTML 만 따로 반환하는 사이트. xhr_html 응답을 list extractor 로 검증해서 통과하면 source URL 을 그 endpoint 로 갱신, fetcher='static' 으로 기록 (배치 시 빠른 정적 GET)
4. **dynamic 페이지 그대로** — fallback 최후. 매 페이지 Playwright 비용 발생

### list_extractor 핵심 ([extractors/list_extractor.py](crawlers/extractors/list_extractor.py))
- `_pick_subjects_for_container`: 컨테이너 내 모든 row 의 anchor 분포 분석 → row 별 unique_ratio 가 가장 높은 anchor fingerprint 우선 채택
  - worldjob 류: 같은 row 안 회사 popup `busiInfoPopup` (회사 단위 unique) vs 채용공고 `goView1` (row 별 unique) → goView1 우선
- `javascript:fnName('id', ...)` anchor → synthetic detail URL `?_jsfn=...&_jsid=...` 생성. base 의 다른 query 는 안 들고옴 (페이지네이션 query 가 같이 들어가서 dedup 깨지던 버그 방지)

---

## 배치 파이프라인 ([crawlers/batch/runner.py](crawlers/batch/runner.py))

```
for site in active sites:
    seen_ids = jobs.external_id WHERE site_id=site.id  ← 이전 batch 까지 본 ID set
    cross_source_seen: set[str] = set()                ← 이번 batch 의 다른 source detail_url

    for source in site.sources:
        if source.fetcher == "api":
            rows = crawl_api(api_schema, already_seen_ids=seen_ids)
        else:
            rows = crawl_list(url, fetcher, already_seen_ids=seen_ids)

        for row in rows:
            if row.detail_url in cross_source_seen: continue   # 다른 메뉴와 중복
            insert_job(...)                                     # raw row 그대로 적재

        cross_source_seen += {row.detail_url for row in rows}
```

### 중요한 dedup 정책 ([list_crawler.py](crawlers/batch/list_crawler.py))
같은 source 안에서:
- **같은 페이지 내 sticky/promoted 중복 → 모두 적재** (worldjob 의 `E20260113003` 가 box 7,8,9 에 sticky 로 3번 → 3 row 적재)
- **다른 페이지에서 같은 ID → cross-page dedup** (siemreap 처럼 `?page=N` 무시하고 같은 결과 반복 사이트 차단)
- 이 페이지의 모든 ID 가 이전 페이지들의 ID set 의 subset 이면 → 페이지네이션 끝/무작동 → break

cross-batch:
- DB 의 `seen_ids` 에 이미 있는 ID 면 row skip (= 이전 배치에서 이미 봤음)
- page 1 에 새 ID 0 개면 → 즉시 break (증분 종료)

cross-source (한 사이트의 여러 메뉴):
- 같은 detail_url 이 이전 source 에 이미 있었으면 skip — runner 의 `cross_source_seen`

### 페이지네이션 학습
1. URL query 에 page param 이 있으면 그것 사용
2. page 1 의 anchor href 들에서 자동 감지 (`?page=2` `?currentPage=2` 같은 패턴)
3. 둘 다 실패 시 **candidate fallback 학습** — `[pageIndex, currentPage, page, pageNum, ...]` 순서로 직접 page 2 호출, 실제로 다른 결과 반환하는 첫 param 채택 (worldjob 처럼 `pageIndex` 만 받는 사이트 자동 학습)

### external_id 추출 ([extractors/external_id.py](crawlers/extractors/external_id.py))
1. path 마지막 숫자 segment (`/view/12345`)
2. 정확 매칭 ID 파라미터 (`?id=`, `?seq=`, `?recruit_id=` 등)
3. ID suffix 패턴 (`?recruitSeq=10434`, `?articleId=...`, `?_jsid=E2026...`)
4. **명시적 제외 키**: pagination (`page`, `currentPage` …), CMS 네비 (`menuId`, `categoryId`, `tabId`, `dobType` …)
5. 그 외 숫자값 → fallback path

---

## 증분 크롤 설계

**채택**: 사이트별 **전체 external_id set** (SELECT 결과를 Python set 으로) 을 매 배치 로드해서 `이미 본 ID 면 skip`.

```python
# runner._existing_external_ids
{r["external_id"] for r in conn.execute(
    "SELECT external_id FROM jobs WHERE site_id = ?", (site_id,)
)}
# DB 자체엔 같은 external_id 의 multiple row 가 있어도 (sticky 적재용)
# Python set 컴프리헨션이 자연스럽게 unique 화 → 멤버십 체크용
```

**dedup 의 두 layer 분리**:
- **cross-batch 증분 (set membership)** — 이전 배치 끝났을 때 DB 의 모든 unique external_id. 새 배치에서 같은 ID 가 list 에 또 보이면 skip
- **같은 batch 내 raw 적재** — 한 페이지 안에 sticky 로 같은 ID 가 N 번 → N 개의 row 적재 (UNIQUE 제약 없음)

이 분리 덕분에 *증분 효율* + *raw entry 보존* 둘 다 가능.

**대안 ("마지막 ID 만 저장 + 그 다음부터") 거부 이유**:
- 비용: SQLite `idx_jobs_site` 인덱스로 사이트당 ~1ms / ~50KB. 1500 사이트 × 600 공고 = 90만 row 라도 set 로드는 batch 전체 시간의 0.01% 미만
- ID 형식 의존성: 마지막 ID 비교는 단조 증가 숫자에만 자연스럽게 작동 (worldjob `E20260428005` 는 사전순, UUID 는 비교 불가)
- sticky/공지 게시물: page 1 상단이 항상 최신순 보장 X
- multi-source: source 별 last_seen 따로 관리해야 → 결국 set 비슷
- "마지막 공고 삭제" fallback 하려면 N 개 stack 보관 = subset of set

**증분 break 트리거** (list_crawler):
- page 1 의 모든 row 의 external_id 가 seen_ids 안에 있으면 → 새 공고 0 → 즉시 break
- pagination 무시 사이트는 cross-page dedup (이번 페이지 ID set 이 이전 페이지들 subset) 에서 추가 break

set 방식은 ID 형식/sticky/삭제 무관. 사이트에서 공고가 사라져도 다음 배치 영향 X (DB 의 공고는 silent ghost, "사라진 공고 신경 안씀" 정책).

---

## 등록된 사이트 (현재 DB 상태)

| site_id | URL 패턴 | 적재 건수 | 비고 |
|---|---|---|---|
| hanin | static + table list | 14 | 기본 케이스 |
| siemreap | static + 광고 도배 보드 | 15 | 1 페이지만 (page 무시 사이트, cross-page dedup 작동) |
| camhr | API mode | 1,711 | API schema 자동 학습, JSON 페이지네이션 |
| hrdkorea | static + JSP table | 580 | URL `?currentPage=N` 페이지네이션 |
| worldjob | static (AJAX HTML endpoint) | 597 | xhr_html 캡처 + js: anchor + `pageIndex` 자동 학습. sticky 多 → unique 509 / raw 597 |

---

## site_id 추출 규칙 ([crawlers/registration/site_id.py](crawlers/registration/site_id.py))

- **ccTLD (.co.kr / .or.kr / …)**: SLD (cctld 직전 rightmost) — 등록 브랜드. `eps.hrdkorea.or.kr → hrdkorea`
- **일반 TLD (.com / .net / …)**: leftmost — 가장 구별성 있는 subdomain. `siemreap.korean.net → siemreap`
- 보일러 라벨 (`www`, `m`, `recruit`, `job`, `career`, `hr` …) 은 skip 하고 적용

테스트: [scripts/debug/check_site_id.py](scripts/debug/check_site_id.py) — 10/10 케이스 검증.

---

## 자동화

```bash
# Windows Task Scheduler — 정각/30분 마다 silent (CMD 창 안 뜸)
python -m scripts.ops.install_schtask

# 내부 흐름:
#   schtasks → wscript "scripts/ops/run_batch_silent.vbs" (SW_HIDE 모드)
#   → "scripts/ops/run_batch_hourly.bat"
#   → python -u -m scripts.ops.run_batch --no-detail
#   → stdout 라인 단위 unbuffered → logs/hourly_batch.log

# 진행 모니터링
tail -f logs/hourly_batch.log

# Slack 알림 (run_batch.py 끝에서)
# :rotating_light: 크롤 완료: 성공 N / 실패 N
# :new: 신규 공고 N
# 사이트별: 사이트명 — 신규 N, 누적 N
```

---

## 라이브 진행 출력

배치 진행 상황은 stdout/log 로 단계별 라인 단위 갱신:
- `[batch] N site(s) — start`
- `[i/N] worldjob (월드잡플러스)  start...`
- `  [worldjob] source 1/1 (static) https://...`
- `    page N fetch...`
- `    page N: total=50 added=47`
- `  [worldjob]   ... 100/594 inserted (+100)`
- `[i/N] worldjob  ok  rows=594 +594 ~0  (10.4s)`

**foreground 실행**: `python -m scripts.ops.run_batch ...` — CMD 즉시 라이브 출력
**schtasks (silent)**: `tail -f logs/hourly_batch.log` 로 다른 창에서 모니터링

---

## 주요 CLI

```bash
# 운영 (ops)
python -m scripts.ops.init_db                                  # DB 스키마 + 마이그레이션
python -m scripts.ops.register https://www.example.com         # 사이트 등록
python -m scripts.ops.register --file urls.txt
python -m scripts.ops.run_batch                                # 모든 active 사이트 배치
python -m scripts.ops.run_batch --site eps                     # 특정 사이트만
python -m scripts.ops.install_schtask                          # 30분 자동 배치 schtasks 등록 (silent)

# DB 관리 (db)
python -m scripts.db.inspect_site <site_id>
python -m scripts.db.remove_site <site_id>                     # site + jobs + runs (FK CASCADE)
python -m scripts.db.wipe_jobs <site_id>                       # jobs 만 (재크롤용)
python -m scripts.db.rename_site_id <old> <new>
python -m scripts.db.dump_names                                # 모든 site name UTF-8 dump
python -m scripts.db.counts                                    # 사이트별 누적 건수
python -m scripts.db.recent_runs                               # 최근 crawl_runs (KST)
python -m scripts.db.close_orphan_runs --minutes 30            # ended_at IS NULL 좀비 마감

# 진단 (debug)
python -m scripts.debug.dry_register <url>                     # dry-run + 분류/검증 디테일
python -m scripts.debug.validate <url>
python -m scripts.debug.discover <url>
python -m scripts.debug.classify <url>
python -m scripts.debug.dump_containers <url> [static|dynamic] # list 후보 컨테이너 전체 dump
python -m scripts.debug.probe_home <url>                       # 홈 fetch (static/dynamic) 비교
python -m scripts.debug.probe_api <url>                        # Playwright XHR 캡처 (JSON + HTML)
python -m scripts.debug.probe_xhr <url>                        # 직접 XHR 호출
python -m scripts.debug.find_list_html <url>                   # HTML 안 list URL 패턴 스캔
python -m scripts.debug.check_site_id                          # site_id 추출 룰 회귀 테스트

# 대시보드 (jobs_total / 사이트 / 공고 / 크롤 이력 — 시간 KST)
streamlit run dashboard/app.py
```

---

## 테스트 통과한 까다로운 케이스

| 케이스 | 사이트 | 해결 |
|---|---|---|
| SPA — 정적 fetch 빈 shell | camhr / hrdkorea / worldjob | discovery + validator 의 dynamic fallback |
| XHR JSON 응답으로 list 로딩 | camhr | API schema 자동 학습, fetcher='api' 페이지네이션 |
| XHR HTML fragment 로 list 로딩 (POST 도) | worldjob | dynamic fetcher 의 `xhr_html` 캡처 (GET+POST), 해당 endpoint 를 source 채택 |
| `javascript:fn('id', ...)` 형식 anchor | worldjob | regex 로 첫 인자 추출, synthetic `?_jsid=...` URL (base 의 query 는 안 들고옴) |
| pagination param 사이트마다 다름 | hrdkorea (`currentPage`), worldjob (`pageIndex`) | page 1 anchor 분석 → 실패 시 candidate param 직접 시도해서 학습 |
| `currentPage=1` / `menuId` 가 ID 로 오인 → 모든 row 충돌 | hrdkorea / worldjob | `_NON_ID_PARAMS` 에 pagination + CMS 네비 키 (menuId, categoryId, dobType …) 명시 제외 |
| 1~2자 회사명 anchor 가 row drop | hrdkorea (17건 누락) | `MIN_LINK_TEXT_LEN: 4 → 2` |
| sidebar 큰 리스트 + 본문 작은 리스트 공존 | worldjob (학교 84개) | 모든 후보 중 임계 통과하는 것 중 큰 것 채택 + nav 안 컨테이너 제외 |
| row 안 회사 popup anchor 가 longest 라 잘못 채택 | worldjob (box 45/47 같은 회사 다른 공고) | `_pick_subjects_for_container` — 컨테이너 단위 anchor 분포 분석, row 별 unique_ratio 가 높은 fingerprint 우선 |
| 메뉴 안내 list 가 채용처럼 보임 | hrdkorea seekAppl.do | `MAX_UNIQUE_PATH_RATIO = 0.5` — detail URL 들 path 모두 다르면 메뉴 |
| 강연/설명회 list 가 채용처럼 보임 | worldjob `stepup/list.do` | `MAX_EVENT_RATIO = 0.5` — 설명회/세미나/강의 키워드 절반 이상이면 reject |
| 사이트가 `?page=N` 무시 → 같은 페이지 무한 반복 | siemreap | cross-page dedup: 이번 페이지의 ID set 이 이전 페이지들 ID 의 subset 이면 break |
| 같은 메뉴 내 sticky 중복 적재 | worldjob (594 vs unique 509) | UNIQUE 제약 제거 — 같은 (site_id, external_id) 의 multiple row 허용 |
| LLM 단일 dict 응답 / unknown 흔들림 | 일반 | 시스템 프롬프트 강화 + 짧은 라벨 + 강 키워드면 override |
| `<title>` SEO 키워드 도배 | camhr | `og:site_name` → `og:title` 첫 segment → `<title>` 가장 짧은 segment |
| schtasks 가 CMD 창 popup | 일반 | wscript SW_HIDE 모드 vbs 래퍼 + `python -u` unbuffered |
| 좀비 run (kill 후 ended_at NULL) | 일반 | `close_orphan_runs.py` — 30분 이상 안 끝난 run 자동 마감. run_batch 시작 시 자동 호출 |
| dashboard 시간 9시간 어긋남 | 일반 | UTC → KST 변환 (`_to_kst`) — sites/jobs/crawl_runs 모두 적용 |

---

## 메모

- DB encoding: SQLite 는 UTF-8 저장. Windows console 이 cp949 라 mojibake 처럼 보이는 경우는 디스플레이 문제 — `python -m scripts.db.dump_names` 같이 파일로 dump 하면 정상.
- Playwright 는 프로세스당 1회 기동 후 재사용 ([dynamic.py:_ensure_browser](crawlers/fetchers/dynamic.py)).
- 회전형 proxy 풀 사용 (403 받은 IP 는 cool-down).
- LLM 비용 절감: classifier 가 `score >= MIN_SCORE = 2` 후보만 분류 대상. snippet 모드 (실제 페이지 일부 fetch) 는 옵션.
- jobs.UNIQUE 제약 제거됐지만 기존 데이터 보존 — `init_db()` 가 자동 마이그레이션 (RENAME → CREATE → INSERT SELECT → DROP).
