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
| **특수** | `naver_cafe` | 네이버 카페 채용 게시판 전용 fetcher (메뉴 게시판별 source 등록, 카페 인증 흐름) | 가벼움 (카페 API 응답 파싱) | `naver_cafe` |

핵심: **3·4단계는 등록할 때만 비싸고 배치는 1단계 수준으로 가벼움.** 2단계가 진짜 비싼 케이스 (배치마다 매번 브라우저). `naver_cafe` 는 fallback 체인 밖의 전용 fetcher.

---

### 현재 등록된 사이트 매핑 (22개)

| site_id | 이름 | 단계 | 누적 jobs | 이유 |
|---|---|---|---|---|
| **mofa** | 재외동포청 | **1단계** static | 10 | 정적 게시판. 공공 사이트라 정적 그대로 fetch 가능 |
| **hanin** | 재캄보디아한인회 | **1단계** static | 14 | 평범한 PHP 게시판 (`bbs/board.php`), HTML 그대로 list 들어있음 |
| **siemreap** | 재캄보디아시엠립한인회 | **1단계** static | 15 | 재캄보디아한인회와 같은 게시판 구조 (`?page=N` 무시 사이트지만 fetch 자체는 static) |
| **jobposting** | 잡포스팅 | **1단계** static | 203 | 정적 HTML list. 단일 page 형태 |
| **hrdkorea** | 한국산업인력공단 고용허가제 통합서비스 | **1단계** static | 580 | JSP `jobRecruit.do` 에 list HTML 그대로 — `currentPage=N` 페이지네이션만 학습 |
| **peoplenjob** | 피플앤잡 | **1단계** static | 10,259 | 정적 HTML list (`/jobs`). 가장 많이 적재된 단일 사이트 |
| **cambojob** | CamboJob | **2단계** dynamic | 610 | anti-scraping 대응 (path-segment 페이지네이션, Referer 검사, 세션 쿠키 필요). static 으로는 차단당해서 배치마다 Playwright 로 가야 함 |
| **superookie** | 슈퍼루키 | **4단계** api + 프록시 회전 | 985 | 처음엔 dynamic+프록시였으나 Playwright capture_api 로 `/api/jobs/search?access_token=...` JSON endpoint 발견 → fetcher=api 로 강등. 프록시 풀 회전 + 무프록시 fallback. 자세한 경위는 §9 참고 |
| **worldjob** | 월드잡플러스 | **3단계** xhr_html → static 으로 저장 | 602 | 메인 페이지는 SPA 라 정적 fetch 가 빈 shell. Playwright 로 띄워서 `getEpmtList.do` 라는 AJAX endpoint 가 list HTML 만 따로 반환하는 걸 발견 → 그 URL 을 source 로 저장하고 fetcher='static' 으로 둠 |
| **camhr** | CamHR | **4단계** api | 1,742 | XHR 응답이 JSON. `/a/job` endpoint + id/title 필드 자동 매칭. 배치는 JSON 페이지네이션으로 수집 |
| **kotrasingapore** | KOTRA 싱가포르 | **특수** naver_cafe | 36 | 네이버 카페 채용게시판 전용 fetcher. 카페 메뉴 단위로 source 등록 |
| **kotravancouver** | KOTRA 캐나다 | **특수** naver_cafe | 189 | 카페 채용게시판 |
| **kotranewyork** | KOTRA 미국 | **특수** naver_cafe | 271 | 카페 채용게시판 |
| **kotradubai** | KOTRA 중동 | **특수** naver_cafe | 282 | 카페 채용게시판 |
| **kotrakualalumpur** | KOTRA 말레이시아 | **특수** naver_cafe | 447 | 카페 채용게시판 |
| **kotranewdelhi** | KOTRA 인도 | **특수** naver_cafe | 713 | 카페 채용게시판 |
| **kotramexico** | KOTRA 중남미 | **특수** naver_cafe | 791 | 카페 채용게시판 |
| **kotrahamburg** | KOTRA 유럽 | **특수** naver_cafe | 806 | 카페 채용게시판 |
| **kotrasydney** | KOTRA 호주·뉴질랜드 | **특수** naver_cafe | 1,033 | 카페 채용게시판 |
| **kotrabeijing** | KOTRA 중국 | **특수** naver_cafe | 1,183 | 카페 채용게시판 |
| **kotratokyo** | KOTRA 일본 | **특수** naver_cafe | 1,265 | 카페 채용게시판 |
| **kotrajakarta1** | KOTRA 인도네시아 | **특수** naver_cafe | 1,366 | 카페 채용게시판 |
| **kotrahochiminh** | KOTRA 호치민 | **특수** naver_cafe | 3,391 | 카페 채용게시판. KOTRA 13개 중 누적 최다 |

---

### 비용 분포 정리

```
1단계 (static)         mofa, hanin, siemreap, jobposting, hrdkorea, peoplenjob   ← 가장 가벼움
3단계 (→ static 저장)  worldjob                                                  ← 등록만 비쌌고 배치는 1단계급
4단계 (api)            camhr                                                     ← 배치는 가벼운 JSON GET
4단계 + 프록시 회전     superookie                                                ← API 강등 후. 프록시 트래픽만 추가 비용
특수 (naver_cafe)      KOTRA 13개 (싱가포르·캐나다·미국·중동·말레이시아·인도·    ← 카페 전용, 가벼움
                       중남미·유럽·호주·중국·일본·인도네시아·호치민)
2단계 (dynamic)        cambojob                                                  ← 배치마다 매번 Playwright (가장 비쌈)
```

**관찰**: 22개 사이트 중 21개가 가벼운 fetcher (1·3·4단계 + naver_cafe) 로 안착. 마지막 1개 cambojob 만 dynamic 에 머물러 있어 배치 비용이 큼 — anti-scraping (path-segment 페이지네이션, Referer 검사, 세션 쿠키) 때문에 어쩔 수 없는 케이스. peoplenjob 은 가장 많은 10,259건을 1단계 static 으로 가져오고 있어 효율 최고. KOTRA 카페 13개는 합쳐서 12,573건으로 단일 사이트 peoplenjob 다음으로 큰 풀.

---

## 9. 슈퍼루키 트래픽 절감 (2026-04-29~30 작업)

### 배경
슈퍼루키는 SPA + IP 차단 사이트라 **dynamic + webshare 프록시 풀 회전** 으로 시작. 프록시는 webshare 무료 계정의 월 1GB 대역폭 한도가 걸려있어, 시간당 배치를 돌리면 단번에 한도 초과 (HTTP 402 Payment Required `bandwidthlimit`). 한도 안에 들어가도록 단계적으로 트래픽 줄임.

### 진단 — 어디서 트래픽이 새는가
| 호출 위치 | 한 회차 트래픽 (개선 전) |
|---|---|
| Playwright 가 페이지당 받는 HTML+JS+CSS+이미지 | ~2.5MB |
| 슈퍼루키 한 회차 (~21페이지 풀크롤) | ~50MB |
| 시간당 배치 × 24시간 × 30일 (최악 가정) | **수 GB ~ 수십 GB / 월** |

대시보드의 "Working" 표시는 IP 살아있음만 검증해서, 실제 402 까지는 안 보였음. `curl -v` 로 직접 확인 시 `X-Webshare-Reason: bandwidthlimit` 헤더가 떨어지는 걸 발견.

### 적용한 조치 (효과 큰 순)

#### 1. **API 직접 호출로 강등** — fetcher: dynamic → api
가장 큰 효과. Playwright 로 슈퍼루키 페이지를 한 번 띄우면서 `capture_api=True` 로 XHR 응답 분석한 결과 `/api/jobs/search?access_token=...` 라는 JSON endpoint 가 list 를 그대로 돌려주는 걸 발견. ApiSchema 작성 후 dynamic → api 로 전환.
- Playwright 자체를 안 띄움 (CPU/메모리/시작 비용 0)
- 페이지당 ~228KB (gzip) JSON 만 받음
- 응답 시간 ~35% 개선

#### 2. **프록시 회전 retry + 무프록시 fallback** — `use_proxy=true` 사이트의 silent 실패 방지
[crawlers/batch/list_crawler.py](crawlers/batch/list_crawler.py) 와 [crawlers/fetchers/api.py](crawlers/fetchers/api.py) 의 use_proxy 경로:
- 풀 안의 프록시 1개가 TUNNEL/PROXY 에러 → 다음 프록시로 자동 회전 retry
- 풀 전체 죽으면 무프록시 fallback 1회 (어떤 사이트는 무프록시도 통과)
- 회전 의미 없는 에러(timeout 등)는 즉시 break

#### 3. **Playwright resource block** — image/font/media/stylesheet abort
슈퍼루키는 api 로 강등되어 무관해졌지만, 다른 dynamic 사이트(cambojob 등) 에는 여전히 적용. `use_proxy=true` 인 사이트는 자동으로 켜짐. 페이지당 트래픽 60–80% 절감 (텍스트 추출 결과엔 영향 없음).

#### 4. **증분 break 강화** — 연속 N 페이지 새 글 0이면 break
기존엔 page 1 끝에서만 break (모든 ID 가 DB 에 있을 때). 새 글 1개라도 잡히면 끝까지(21~22페이지) 풀크롤하는 문제. `empty_streak` 카운터 도입해서 연속 2페이지 동안 added=0 이면 종료. 풀크롤 트래픽이 ~50MB → ~5MB 로 감소 (date desc 정렬 사이트 가정).

### 추정 효과 (시간당 배치 × 30일)
| 단계 | 풀크롤 회차 | 라이트 회차 | 월 트래픽 |
|---|---|---|---|
| 무수정 (dynamic+proxy) | ~50MB × 150회 | ~3MB × 570회 | **~9GB** ❌ |
| + resource block + streak break | ~2MB × 150회 | ~0.5MB × 570회 | ~590MB ✅ |
| **+ api 전환 (현재)** | ~0.7MB × 150회 | ~0.23MB × 570회 | **~280MB** ✅✅ |

### 알아둘 것 (운영 위험)
- **access_token** 은 슈퍼루키 서버 발급 정적 키. URL 의 `?access_token=...` 에 박혀있음. 만료/변경 시 슈퍼루키 API 가 401/403 반환 → Playwright 로 페이지 재캡쳐해서 새 token 추출 후 ApiSchema 갱신 필요. (dynamic 코드는 그대로 남아있어 폴백 가능)
- **프록시 (webshare)** 는 token 과 별개. 무료 1GB 한도 소진 시 402(bandwidthlimit), IP 살아있어도 트래픽 거부. 새 계정/plan 으로 갱신 (`.env` 의 `PROXIES=` 갱신).
