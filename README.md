# JobCrawler

채용 사이트를 자동 등록 + 주기 배치로 크롤링하는 파이프라인.
사람 개입을 최대한 배제하고 홈 URL 한 줄만 주면 사이트의 모든 공고를 모아온다.

---

## 흐름 한 줄 요약

```
[등록] 홈 URL → 메뉴 탐색 → LLM 분류 → 검증 → dedupe → sites.sources[] 저장
[배치] sources[] 순회 → 페이지네이션 따라 list 수집 → 신규 ID 만 jobs 적재
```

---

## 디렉토리

| path | 역할 |
|---|---|
| [crawlers/registration/](crawlers/registration/) | 홈 → 사이트 등록 (discover/classify/validate/dedupe/register) |
| [crawlers/batch/](crawlers/batch/) | 등록된 source URL 무조건 크롤링 (runner/list_crawler/detail_crawler) |
| [crawlers/extractors/](crawlers/extractors/) | HTML → list/detail/external_id, API schema 자동 학습 |
| [crawlers/fetchers/](crawlers/fetchers/) | static (curl_cffi), dynamic (Playwright + XHR 캡처), api |
| [crawlers/infra/](crawlers/infra/) | SQLite 스키마 + repos |
| [scripts/ops/](scripts/ops/) · [scripts/db/](scripts/db/) · [scripts/debug/](scripts/debug/) | CLI 진입점 — 운영 / DB 관리 / 진단 (아래 *주요 CLI* 참고) |
| [dashboard/](dashboard/) | Streamlit 대시보드 |
| data/crawler.db | SQLite (sites / jobs / crawl_runs) |

---

## DB 스키마 ([crawlers/infra/db.py](crawlers/infra/db.py))

- **sites** — `id (site_id)`, `home_url`, `name`, `status` (`pending|active|paused|dead`), `sources` (JSON), `consecutive_failures`, …
- **jobs** — `id`, `site_id` (FK), `external_id`, `url`, `title`, `raw`, `first_seen_at`, `last_seen_at`. `UNIQUE(site_id, external_id)`.
- **crawl_runs** — 매 배치 1 row. `result`, `jobs_added/updated/unchanged`, `rows_seen`, `error`.

---

## 등록 파이프라인

각 사이트별 최초 등록 시, 한번만 실행.
전체 메뉴 판별(구인공고가 있는 메뉴인지 아닌지) 후, DB에 URL 저장.

| 단계 | 모듈 | 핵심 |
|---|---|---|
| 1. discover | [menu_discovery.py](crawlers/registration/menu_discovery.py) | 홈 fetch → `<a>` 키워드 스코어링. SPA 면 dynamic fallback |
| 2. classify | [menu_classifier.py](crawlers/registration/menu_classifier.py) | LLM (gpt-4o-mini) 으로 `full / filtered / personal / unknown` 분류. 짧은 라벨 + 강 키워드면 unknown→filtered override |
| 3. validate | [menu_validator.py](crawlers/registration/menu_validator.py) | 임계 통과 시 list 인정. static→dynamic→XHR HTML→API schema 순서로 fallback |
| 4. dedupe | [menu_dedupe.py](crawlers/registration/menu_dedupe.py) | 중복 메뉴 흡수 |
| 5. save | [register.py](crawlers/registration/register.py) → upsert_site | sources[] 저장. 검증 0 통과면 `pending` |

### 검증 임계
- `MIN_ROWS = 2`, `MIN_SUBJ_RATIO = 0.5` (행마다 detail anchor 비율)
- `MIN_JOB_TITLE_RATIO = 0.3` 또는 `MIN_JOB_TITLE_COUNT = 2` — 채용 키워드. row의 `title` 만 아니라 `row_text` (인접 td 분야/직종 등) 도 검사
- `MAX_UNIQUE_PATH_RATIO = 0.5` — detail URL 들이 모두 다른 path 면 list 가 아닌 메뉴
- `<nav>/<aside>/<header>/<footer>` 안에 있는 컨테이너는 후보에서 제외
- 모든 후보 컨테이너 중 통과하는 것 중 가장 큰 것 채택 (sidebar 84-row 학교목록 + 본문 10-row 채용 list 공존 케이스 대응)

### dynamic fallback 전략
1. 정적 fetch 가 임계 미달 → Playwright 로 재시도
2. dynamic 도 미달 → `xhr_html` 응답 (AJAX 로 list 만 따로 받는 사이트, 예: worldjob.or.kr) 스캔. 통과하면 그 endpoint 를 source URL 로 저장
3. JSON XHR 캡처 → API schema 자동 학습 (camhr.com 케이스). schema.to_dict() 가 source 에 저장되고 batch 가 `crawl_api()` 로 페이지네이션 호출
4. `javascript:fnName('id', ...)` 형식 anchor 도 인식 — 첫 인자를 ID 로 보고 synthetic detail URL `?_jsfn=...&_jsid=...` 생성

---

## 배치 파이프라인 ([crawlers/batch/runner.py](crawlers/batch/runner.py))

```
for site in active sites:
    already_seen = jobs.external_id WHERE site_id=site.id  ← 이미 본 ID set
    for source in site.sources:
        if source.fetcher == "api":
            rows = crawl_api(api_schema, already_seen)
        else:
            rows = crawl_list(url, fetcher, already_seen)
        upsert each row
```

### 페이지네이션 ([list_crawler.py](crawlers/batch/list_crawler.py))
1. page 1 fetch → 모든 list 후보 컨테이너에서 detail URL 수집
2. 가장 빈도 높은 prefix 학습 (예: `https://eps.../jobRecruit.do`)
3. prefix 매칭 row 가 가장 많은 컨테이너 1개 lock (signature)
4. page 2+ : 같은 signature + 같은 prefix 매칭만 채택
5. **page 1 의 row 가 모두 already_seen 이면 즉시 break** (증분 종료)
6. pagination param 자동 감지: URL query 에 `page/currentPage/...` 없으면 page 1 의 anchor href 들에서 추론

### external_id 추출 ([extractors/external_id.py](crawlers/extractors/external_id.py))
1. path 마지막 숫자 segment (`/view/12345`)
2. ID-named query 파라미터 (`?seq=12345`)
3. ID suffix 패턴 (`?recruitSeq=10434`, `?articleId=...`)
4. pagination 키 (`page`, `currentPage` 등) 는 명시적으로 제외
5. fallback: 정규화된 URL

---

## 증분 크롤 설계 결정 (전체 ID set 로드 vs 마지막 ID 만)

**채택**: 사이트별 **전체 external_id set** 을 매 배치 로드해서 `이미 본 ID 면 skip`.

**대안 ("마지막 ID 만 저장 + 그 다음부터") 거부 이유**:
- *비용*: SQLite `idx_jobs_site` 인덱스로 사이트당 ~1ms / ~50KB. 1500 사이트 × 600 공고 = 90만 row 라도 set 로드는 batch 전체 시간의 0.01% 미만. 진짜 병목은 HTTP/Playwright (페이지당 1~5s).
- *ID 형식 의존성*: 마지막 ID 비교는 단조 증가 숫자에만 자연스럽게 작동. `recruitSeq=10434` 는 OK 지만 `E20260428005` (날짜+카운터), UUID/hash 형 ID 는 비교 불가.
- *sticky/공지 게시물*: page 1 상단이 항상 최신순 보장 X. "마지막 ID 다음" 경계가 sticky 와 신규 사이에서 잘못 잡힘.
- *multi-source*: 한 사이트에 source 여러개면 각 source 별 last_seen 따로 관리 → 결국 set 비슷해짐.
- *마지막 공고 삭제*: "그 전 ID 로 fallback" 하려면 N 개 stack 보관 = subset of set. 여기까지 오면 set 의 단순성을 잃음.

**현재 방식**: set membership check 는 ID 형식/sticky/삭제 무관. "본 적 없는 ID 면 저장" 단일 규칙. 사이트에서 공고가 삭제돼도 다음 배치 영향 X (DB 의 공고는 silent ghost 로 유지, 사용자 요구 "사라진 공고 신경 안 씀" 부합).

**증분 종료 조건**: page 1 의 모든 row 가 already_seen 이면 break. 채용 사이트 99% 가 최신순 정렬이라 실용 문제 없음.

---

## 등록된 사이트 (현재까지 검증된 패턴)

| site_id | 패턴 | 비고 |
|---|---|---|
| hanin | static + table list | 기본 케이스 |
| siemreap | static + 광고 도배 보드 | `MIN_JOB_TITLE_COUNT=2` 로 통과 |
| camhr | SPA + JSON API 자동 학습 | API schema, 1698건 |
| hrdkorea | static + JSP table 페이지네이션 | 580건 |
| worldjob | dynamic + AJAX HTML fragment | xhr_html 캡처 + js: anchor 변환 |

---

## site_id 추출 규칙 ([crawlers/registration/site_id.py](crawlers/registration/site_id.py))

- **ccTLD (.co.kr / .or.kr / …)**: SLD (cctld 직전 rightmost) — 등록 브랜드. `eps.hrdkorea.or.kr → hrdkorea`
- **일반 TLD (.com / .net / …)**: leftmost — 가장 구별성 있는 subdomain. `siemreap.korean.net → siemreap`
- 보일러 라벨 (`www`, `m`, `recruit`, `job`, `career`, `hr` …) 은 skip 하고 적용

테스트: [scripts/debug/check_site_id.py](scripts/debug/check_site_id.py) — 10/10 케이스 검증.

---

## 자동화

```bash
# Windows Task Scheduler (정각/30분 마다)
schtasks /Create /SC MINUTE /MO 30 /TN JobCrawler_HalfHourlyBatch /TR scripts\run_batch_hourly.bat

# Slack 알림: scripts/ops/run_batch.py 끝에서
# :rotating_light: 크롤 완료: 성공 N / 실패 N
# :new: 신규 공고 N
# 사이트별: 사이트명 — 신규 N, 누적 N
```

---

## 주요 CLI

scripts/ 는 용도별 3개 폴더로 구성:

```
scripts/
  ops/    — 운영 진입점 (DB 초기화, 등록, 배치, schtasks 설치)
  db/     — DB 관리 (inspect/remove/wipe/rename)
  debug/  — 진단·디버그 (validate, discover, classify, probe, dump…)
```

```bash
# 운영 (ops)
python -m scripts.ops.init_db                                  # DB 스키마 생성
python -m scripts.ops.register https://www.example.com         # 사이트 등록
python -m scripts.ops.register --file urls.txt
python -m scripts.ops.run_batch                                # 모든 active 사이트 배치
python -m scripts.ops.run_batch --site eps                     # 특정 사이트만
python -m scripts.ops.install_schtask                          # 30분 자동 배치 schtasks 등록 (CMD 안 뜸)

# DB 관리 (db)
python -m scripts.db.inspect_site <site_id>
python -m scripts.db.remove_site <site_id>                     # site + jobs + runs (FK CASCADE)
python -m scripts.db.wipe_jobs <site_id>                       # jobs 만 (재크롤용)
python -m scripts.db.rename_site_id <old> <new>
python -m scripts.db.dump_names                                # 모든 site name UTF-8 dump

# 진단 (debug)
python -m scripts.debug.dry_register <url>                     # dry-run + 분류/검증 디테일
python -m scripts.debug.validate <url>
python -m scripts.debug.discover <url>
python -m scripts.debug.classify <url>
python -m scripts.debug.dump_containers <url> [static|dynamic] # list 후보 컨테이너 전체 dump
python -m scripts.debug.probe_home <url>                       # 홈 fetch (static/dynamic) 비교
python -m scripts.debug.probe_api <url>                        # Playwright XHR 캡처
python -m scripts.debug.probe_xhr <url>                        # 직접 XHR 호출
python -m scripts.debug.find_list_html <url>                   # HTML 안 list URL 패턴 스캔
python -m scripts.debug.check_site_id                          # site_id 추출 룰 회귀 테스트

# 대시보드
streamlit run dashboard/app.py
```

---

## 테스트 통과한 까다로운 케이스

| 케이스 | 사이트 | 해결 |
|---|---|---|
| SPA — 정적 fetch 빈 shell | camhr / hrdkorea / worldjob | discovery + validator 의 dynamic fallback |
| XHR JSON 응답으로 list 로딩 | camhr | API schema 자동 학습, fetcher='api' 페이지네이션 |
| XHR HTML fragment 로 list 로딩 | worldjob | dynamic fetcher 의 `xhr_html` 캡처, 해당 endpoint 를 source 채택 |
| `javascript:fn('id', ...)` 형식 anchor | worldjob | regex 로 첫 인자 추출, synthetic `?_jsid=...` URL |
| pagination param 사이트마다 다름 | hrdkorea (currentPage) | page 1 anchor 분석으로 자동 추론 |
| `currentPage=1` 이 ID 로 오인 → 모든 row 충돌 | hrdkorea | `_NON_ID_PARAMS` 제외 + `_ID_SUFFIX_RE` (recruitSeq, articleId, ...) 매칭 |
| 1~2자 회사명 anchor 가 row drop | hrdkorea (17건 누락) | `MIN_LINK_TEXT_LEN: 4 → 2` |
| sidebar 큰 리스트 + 본문 작은 리스트 공존 | worldjob (학교 84개) | 모든 후보 중 통과하는 것 중 큰 것 채택 + nav 안 컨테이너 제외 |
| 메뉴 안내 list 가 채용처럼 보임 | hrdkorea seekAppl.do | `MAX_UNIQUE_PATH_RATIO = 0.5` — detail URL 들 path 모두 다르면 메뉴 |
| LLM 단일 dict 응답 / unknown 흔들림 | 일반 | 시스템 프롬프트 강화 + 짧은 라벨 + 강 키워드면 override |
| `<title>` SEO 키워드 도배 | camhr | `og:site_name` → `og:title` 첫 segment → `<title>` 가장 짧은 segment |

---

## 메모

- DB encoding: SQLite 는 UTF-8 저장. Windows console 이 cp949 라 mojibake 처럼 보이는 경우는 디스플레이 문제 — `data/site_names.txt` 같이 파일로 dump 하면 정상.
- Playwright 는 프로세스당 1회 기동 후 재사용 ([dynamic.py:_ensure_browser](crawlers/fetchers/dynamic.py)).
- 회전형 proxy 풀 사용 (403 받은 IP 는 cool-down).
- LLM 비용 절감: classifier 가 `score >= MIN_SCORE = 2` 후보만 분류 대상. snippet 모드 (실제 페이지 일부 fetch) 는 옵션.
