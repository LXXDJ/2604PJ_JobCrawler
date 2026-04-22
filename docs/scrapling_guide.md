# Scrapling 크롤링 참고 가이드

> 이 문서의 목적: Scrapling 라이브러리로 크롤러를 만들 때 빠르게 참고할 수 있는 치트시트입니다.
> "내가 지금 어떤 상황이면 뭘 써야 하지?" 를 빠르게 답할 수 있도록 구성했습니다.

---

## 📌 목차

1. [크롤링의 기본 3단계](#1-크롤링의-기본-3단계)
2. [Fetcher 선택 의사결정 (가장 중요)](#2-fetcher-선택-의사결정)
3. [Fetcher별 사용법](#3-fetcher별-사용법)
4. [봇 탐지 우회 원리와 옵션](#4-봇-탐지-우회-원리와-옵션)
5. [Cloudflare 우회](#5-cloudflare-우회)
6. [프록시 로테이션](#6-프록시-로테이션)
7. [비동기/병렬 크롤링](#7-비동기병렬-크롤링)
8. [스파이더 (대량 크롤링)](#8-스파이더-대량-크롤링)
9. [에러 대응 체크리스트](#9-에러-대응-체크리스트)
10. [실전 코드 템플릿](#10-실전-코드-템플릿)
11. [윤리/법적 주의사항](#11-윤리법적-주의사항)

---

## 1. 크롤링의 기본 3단계

```
[1] 요청 보내기  →  [2] 응답 받기  →  [3] 원하는 정보만 뽑기
   (Request)       (Response)        (Parsing)
```

**핵심 용어**:
- **HTTP**: 브라우저와 서버가 대화하는 규칙
- **HTML**: 웹페이지의 실제 내용 (태그로 구성된 문서)
- **User-Agent**: "내가 어떤 브라우저다"를 알리는 명찰
- **Referer**: "어디서 타고 들어왔다"를 알리는 정보
- **Cookie**: 로그인 상태 등을 기억하는 데이터

---

## 2. Fetcher 선택 의사결정

Scrapling은 3가지 Fetcher를 제공합니다. **위에서 아래로** 시도하고, 안 되면 한 단계 내려오세요.

| Fetcher | 파일 | 속도 | 탐지 회피 | 언제 쓰나 |
|---------|------|------|----------|----------|
| `Fetcher` | `fetchers/requests.py` | ⚡️ 매우 빠름 | ❌ 낮음 | 정적 HTML, 간단한 사이트 |
| `DynamicFetcher` | `fetchers/chrome.py` | 🐢 느림 | ⚠️ 중간 | JavaScript 렌더링 필요한 사이트 |
| `StealthyFetcher` | `fetchers/stealth_chrome.py` | 🐢🐢 매우 느림 | 🛡️ 높음 | 봇 탐지/Cloudflare 있는 사이트 |

### 의사결정 플로우차트

```
Q1. 타겟 페이지의 HTML 소스 보기(Ctrl+U)에 원하는 데이터가 보이는가?
├─ YES → [Fetcher] 사용 (가장 빠름)
└─ NO → JavaScript로 동적 로딩되는 사이트
        ↓
        Q2. 접속해도 403 / "Access Denied" / Cloudflare 페이지가 뜨는가?
        ├─ NO  → [DynamicFetcher] 사용
        └─ YES → [StealthyFetcher] 사용 + solve_cloudflare=True
```

---

## 3. Fetcher별 사용법

### 3-1. `Fetcher` — 단순 HTTP 요청

```python
from scrapling.fetchers import Fetcher

response = Fetcher.get(
    "https://example.com",
    headers={"User-Agent": "Mozilla/5.0 ..."},
    cookies={"session": "abc"},
    stealthy_headers=True,   # 기본 탐지 우회 헤더
    follow_redirects=True,
    timeout=30,
)

print(response.status)           # 200
print(response.html_content[:500])
```

**장점**: 초당 수백 건 가능. 자원 적게 씀.
**단점**: JavaScript 실행 불가. 70% 이상 현대 사이트에선 안 됨.

### 3-2. `DynamicFetcher` — 브라우저 자동화

```python
from scrapling.fetchers import DynamicFetcher

response = DynamicFetcher.fetch(
    "https://example.com",
    headless=True,                   # 창 안 띄움 (False면 화면에 보임)
    network_idle=True,               # 모든 네트워크 요청 끝날 때까지 대기
    wait_selector=".product-list",   # 이 요소 나올 때까지 대기
    wait_selector_state="visible",
    disable_resources=True,          # 이미지/폰트 스킵 → 속도 UP
    useragent="Mozilla/5.0 ...",
    timeout=30000,                   # ms 단위
)
```

**페이지 로드 후 자동화** (스크롤, 클릭 등):
```python
def my_action(page):
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)
    page.click(".load-more")

response = DynamicFetcher.fetch("https://...", page_action=my_action)
```

### 3-3. `StealthyFetcher` — 스텔스 크롬

```python
from scrapling.fetchers import StealthyFetcher

response = StealthyFetcher.fetch(
    "https://example.com",
    headless=True,
    solve_cloudflare=True,       # Cloudflare 체크박스 자동 풀이
    hide_canvas=True,            # Canvas 지문 위장
    block_webrtc=True,           # WebRTC로 IP 유출 방지
    allow_webgl=True,            # WebGL은 켜둬야 의심 덜 받음
    google_search=True,          # "Google에서 타고 왔어요" 위장
    real_chrome=False,           # True면 내 PC의 진짜 Chrome 사용
    network_idle=True,
)
```

---

## 4. 봇 탐지 우회 원리와 옵션

### 왜 우회가 필요한가?
사이트는 "브라우저 지문(fingerprint)"으로 사람/봇을 구분합니다.
너무 완벽해도 의심, 너무 기본값이어도 의심 → **"보통 사람의 브라우저"처럼** 보여야 함.

### 주요 지문과 대응

| 지문 | 위험 | Scrapling 옵션 |
|------|------|----------------|
| **WebRTC** | 프록시 써도 진짜 IP 유출 | `block_webrtc=True` |
| **Canvas** | 그림 그리는 방식이 PC마다 달라서 고유 식별자로 쓰임 | `hide_canvas=True` |
| **WebGL** | 3D 기능 지문 | `allow_webgl=True` (**끄면 오히려 의심**) |
| **User-Agent** | 봇 기본값이면 즉시 탐지 | `useragent=` 지정 |
| **Referer** | 구글 검색 타고 온 것처럼 보이면 자연스러움 | `google_search=True` |
| **navigator.webdriver** | 자동화 도구의 표식 | Patchright(내부 사용)가 자동 제거 |
| **Locale/Timezone** | 프록시는 미국인데 언어가 한국어면 의심 | `locale=`, `timezone_id=` 지정 |

### 권장 설정 (빡센 사이트용)

```python
StealthyFetcher.fetch(
    url,
    headless=True,
    hide_canvas=True,
    block_webrtc=True,
    allow_webgl=True,
    google_search=True,
    solve_cloudflare=True,
    network_idle=True,
    useragent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ...",
    locale="ko-KR",
    timezone_id="Asia/Seoul",
)
```

---

## 5. Cloudflare 우회

### Cloudflare는 2단계로 검사

**1단계: 조용히 지문 검사** (사용자는 모름)
→ 위의 "봇 탐지 우회 옵션"으로 통과

**2단계: "사람임을 증명해봐" 체크박스(Turnstile)**
→ `solve_cloudflare=True` 옵션이 자동 처리

### 4가지 Turnstile 챌린지 타입

| 타입 | 설명 | 대응 |
|------|------|------|
| `non-interactive` | 클릭 불필요, 자동 통과 | 기다림 |
| `managed` | 체크박스 클릭 필요 | 랜덤 좌표/딜레이로 클릭 |
| `interactive` | 체크박스 클릭 필요 | 위와 동일 |
| `embedded` | 페이지 내부에 숨김 | 위와 동일 |

**핵심 우회 기법**:
- 정확한 중앙이 아닌 **랜덤 좌표**(+26~28픽셀) 클릭
- 클릭 시간도 **100~200ms 랜덤 딜레이**
- 인간은 정확한 반복이 불가능하니 이게 통과의 핵심

### 사용법

```python
response = StealthyFetcher.fetch(
    "https://cloudflare-protected.com",
    solve_cloudflare=True,   # 이게 전부
    headless=True,
)
```

---

## 6. 프록시 로테이션

### 왜 필요?
같은 IP에서 과도한 요청 → 차단. 여러 IP로 분산 필요.

### 단일 프록시 사용

```python
# 문자열 형식
response = Fetcher.get(url, proxy="http://user:pass@proxy.com:8080")

# 딕셔너리 형식 (Playwright 표준)
response = DynamicFetcher.fetch(url, proxy={
    "server": "http://proxy.com:8080",
    "username": "user",
    "password": "pass",
})
```

### 로테이터 사용

```python
from scrapling.engines.toolbelt.proxy_rotation import ProxyRotator

rotator = ProxyRotator([
    "http://user:pass@proxy1.com:8080",
    "http://user:pass@proxy2.com:8080",
    "http://user:pass@proxy3.com:8080",
])
# Scrapling 세션에 전달 (StealthySession 등에서 자동 사용)
```

### 프록시 선택 팁

| 종류 | 품질 | 가격 | 용도 |
|------|------|------|------|
| Datacenter | ⚠️ 쉽게 차단 | 싸다 | 일반 사이트 |
| Residential | 🛡️ 탐지 회피 좋음 | 10배 비쌈 | 빡센 사이트 |
| Mobile | 🛡️🛡️ 최고 | 훨씬 비쌈 | 최상위 빡센 사이트 |

**추천 서비스**: Bright Data, Oxylabs, Smartproxy, Webshare
**주의**: 무료 프록시는 99%가 차단되거나 위험합니다. 쓰지 마세요.

---

## 7. 비동기/병렬 크롤링

### 동기 vs 비동기 이해

```python
# 동기: 하나씩 순서대로 (URL 100개 × 5초 = 500초)
for url in urls:
    response = Fetcher.get(url)

# 비동기: 동시에 여러 개 (URL 100개를 10개씩 병렬 = 50초)
import asyncio
async def crawl():
    tasks = [async_fetch(url) for url in urls]
    return await asyncio.gather(*tasks)
```

### `AsyncDynamicSession` 사용 예시

```python
import asyncio
from scrapling.engines._browsers._controllers import AsyncDynamicSession

async def crawl_many():
    async with AsyncDynamicSession(max_pages=10, headless=True) as session:
        urls = [f"https://example.com/{i}" for i in range(100)]
        responses = await asyncio.gather(
            *[session.fetch(url) for url in urls]
        )
        return responses

results = asyncio.run(crawl_many())
```

### `max_pages` 권장값

| PC 사양 | 권장 max_pages |
|---------|---------------|
| 메모리 8GB | 3~5 |
| 메모리 16GB | 5~10 |
| 메모리 32GB+ | 10~20 |
| 서버 환경 | 20~50 |

⚠️ 너무 높이면 메모리 터짐. 너무 빠르면 사이트 차단.

---

## 8. 스파이더 (대량 크롤링)

### 언제 쓰나?
- URL 1000개 이상
- 여러 페이지 자동 순회 (리스트 → 상세 → 다음 페이지)
- 중단/재개 필요
- 자동 재시도 / 중복 방지 / 통계 필요

### 기본 구조

```python
from scrapling.spiders import Spider
from scrapling.spiders.request import Request
from scrapling.fetchers import FetcherSession

class JobSpider(Spider):
    name = "job_spider"
    start_urls = ["https://jobs.example.com/list?page=1"]
    allowed_domains = {"jobs.example.com"}
    
    # 동시 요청 수
    concurrent_requests = 4
    concurrent_requests_per_domain = 2
    
    # 요청 간 딜레이 (매너 있게)
    download_delay = 1.0
    
    # 차단 시 재시도 횟수
    max_blocked_retries = 3
    
    # robots.txt 준수 여부
    robots_txt_obey = True
    
    def configure_sessions(self, manager):
        manager.add("default", FetcherSession(stealthy_headers=True))
    
    async def parse(self, response):
        # 리스트 페이지에서 상세 페이지 URL 추출
        for job in response.css(".job-card"):
            detail_url = job.css("a::attr(href)").get()
            yield Request(detail_url, callback=self.parse_detail)
        
        # 다음 페이지
        next_url = response.css("a.next::attr(href)").get()
        if next_url:
            yield Request(next_url)  # self.parse가 기본 callback
    
    async def parse_detail(self, response):
        # 상세 페이지에서 데이터 추출
        yield {
            "title": response.css("h1::text").get(),
            "company": response.css(".company::text").get(),
            "salary": response.css(".salary::text").get(),
            "url": response.url,
        }
```

### 체크포인트 (중단/재개)

```python
spider = JobSpider(
    crawldir="./checkpoints",   # 진행 상황 저장 폴더
    interval=300.0,              # 5분마다 저장
)
# Ctrl+C로 중단 → 다시 실행하면 이어서 크롤링
```

### 자동 차단 감지

```python
BLOCKED_CODES = {401, 403, 407, 429, 444, 500, 502, 503, 504}
```
이 코드들이 오면 자동으로 "차단됨" 판단 → `retry_blocked_request()` 호출해서 재시도.
커스텀 로직 원하면 `is_blocked()` 오버라이드.

### 스파이더 내부 구성

| 파일 | 역할 |
|------|------|
| `spider.py` | 사용자가 상속하는 기본 클래스 |
| `engine.py` | 크롤링 엔진 (실행 루프) |
| `scheduler.py` | 요청 큐 관리 |
| `session.py` | Fetcher 여러 개 관리 |
| `checkpoint.py` | 진행 상황 저장/복원 |
| `cache.py` | 개발 중 응답 캐싱 |
| `robotstxt.py` | robots.txt 체크 |

---

## 9. 에러 대응 체크리스트

| 증상 | 원인 | 해결 |
|------|------|------|
| `403 Forbidden` | 봇으로 찍힘 | User-Agent 변경 → 그래도 안 되면 `StealthyFetcher` |
| `429 Too Many Requests` | 요청 너무 빠름 | `download_delay` 늘리기, 프록시 로테이션 |
| Cloudflare "Just a moment..." | Cloudflare 보호 | `StealthyFetcher` + `solve_cloudflare=True` |
| 빈 HTML / 데이터 없음 | JavaScript 렌더링 필요 | `DynamicFetcher` 또는 `StealthyFetcher` + `wait_selector` |
| 간헐적 타임아웃 | 프록시 불안정 | `timeout` 늘리기, `retries` 활용 |
| IP 차단 | 같은 IP 과도 사용 | 프록시 로테이션 (Residential 추천) |
| HTML 구조 변경으로 Selector 깨짐 | 사이트 개편 | Scrapling의 **Auto-match** 기능 활용 |
| 메모리 부족 | `max_pages` 너무 큼 | `max_pages` 줄이기, `disable_resources=True` |

---

## 10. 실전 코드 템플릿

### 템플릿 1: 가장 단순한 크롤러

```python
from scrapling.fetchers import Fetcher

response = Fetcher.get("https://example.com", stealthy_headers=True)
titles = response.css("h2.title::text").getall()
print(titles)
```

### 템플릿 2: 로그인 후 크롤링

```python
from scrapling.engines._browsers._controllers import DynamicSession

with DynamicSession(
    headless=False,  # 처음엔 False로 해서 직접 로그인
    user_data_dir="./my_profile",  # 쿠키 유지
) as session:
    # 첫 실행: 수동 로그인
    response = session.fetch("https://site.com/login")
    # 이후 실행: 쿠키 유지되어 자동 로그인됨
    data = session.fetch("https://site.com/my-data")
```

### 템플릿 3: 빡센 사이트 크롤링

```python
from scrapling.fetchers import StealthyFetcher

response = StealthyFetcher.fetch(
    "https://protected-site.com",
    headless=True,
    solve_cloudflare=True,
    hide_canvas=True,
    block_webrtc=True,
    google_search=True,
    locale="ko-KR",
    timezone_id="Asia/Seoul",
    network_idle=True,
    wait_selector=".target-data",
    proxy="http://user:pass@residential-proxy.com:8080",
)
```

### 템플릿 4: 대량 비동기 크롤링

```python
import asyncio
from scrapling.engines._browsers._controllers import AsyncDynamicSession

async def main():
    urls = [...]  # 수백~수천 개
    async with AsyncDynamicSession(
        max_pages=8,
        headless=True,
        disable_resources=True,
    ) as session:
        results = await asyncio.gather(
            *[session.fetch(url) for url in urls],
            return_exceptions=True,  # 하나 실패해도 나머지 진행
        )
    
    successes = [r for r in results if not isinstance(r, Exception)]
    failures = [r for r in results if isinstance(r, Exception)]
    print(f"성공: {len(successes)}, 실패: {len(failures)}")

asyncio.run(main())
```

### 템플릿 5: 채용공고 수집 스파이더

```python
from scrapling.spiders import Spider
from scrapling.spiders.request import Request
from scrapling.fetchers import FetcherSession

class JobListingSpider(Spider):
    name = "job_listings"
    start_urls = ["https://example-jobs.com/?page=1"]
    allowed_domains = {"example-jobs.com"}
    concurrent_requests = 4
    download_delay = 1.5
    robots_txt_obey = True
    
    def configure_sessions(self, manager):
        manager.add("default", FetcherSession(stealthy_headers=True))
    
    async def parse(self, response):
        for card in response.css(".job-listing"):
            url = card.css("a::attr(href)").get()
            if url:
                yield Request(response.urljoin(url), callback=self.parse_job)
        
        # 페이지네이션
        next_page = response.css("a.pagination-next::attr(href)").get()
        if next_page:
            yield Request(response.urljoin(next_page))
    
    async def parse_job(self, response):
        yield {
            "title": response.css("h1.job-title::text").get(),
            "company": response.css(".company-name::text").get(),
            "location": response.css(".location::text").get(),
            "salary": response.css(".salary::text").get(),
            "description": response.css(".description").get_text(),
            "url": response.url,
            "scraped_at": response.meta.get("timestamp"),
        }

# 실행
spider = JobListingSpider(crawldir="./checkpoints")
# 스파이더 실행 코드는 engine.py 참고
```

---

## 11. 윤리/법적 주의사항

### 반드시 지켜야 할 것
- ✅ **robots.txt 확인** — `https://사이트주소/robots.txt` 에서 크롤링 허용 범위 확인
- ✅ **이용약관 읽기** — 많은 사이트가 ToS에 크롤링 금지 명시
- ✅ **적절한 딜레이** — 초당 수십 건 때리지 말 것 (`download_delay` 최소 1초 권장)
- ✅ **User-Agent에 연락처 기재** — 프로급 크롤러는 "MyBot/1.0 (contact@email.com)" 형식
- ✅ **개인정보 수집 시 관련 법 준수** — 한국은 개인정보보호법

### 피해야 할 것
- ❌ **로그인 필요 데이터의 대량 수집** — 대부분 ToS 위반
- ❌ **유료 콘텐츠 우회** — 저작권 문제
- ❌ **과도한 동시 요청** — 사이트 서버에 부담 = DDoS로 간주될 수 있음
- ❌ **CAPTCHA 대량 우회** — 명백한 금지 의사 무시
- ❌ **수집한 데이터의 재판매** — 저작권/데이터베이스권 문제

### 법적 참고
- 한국: "컴퓨터 등 사용 사기" 죄 (형법 347조의2)에 해당할 수 있음
- 해외: 미국 CFAA, EU GDPR 등
- 판례: **네이버 vs 엔하위키** 등 (한국 대법원 판례 존재)

**원칙**: "사이트 운영에 부담 주지 않고, 공개된 정보만, 합법 목적으로" — 이 3가지만 지키면 대부분 안전합니다.

---

## 📚 추가 참고

- **Scrapling 공식 문서**: https://scrapling.readthedocs.io/
- **Playwright 문서**: https://playwright.dev/python/
- **프로젝트 소스코드 위치**:
  - Fetcher: `scrapling/fetchers/`
  - 엔진: `scrapling/engines/_browsers/`
  - 스파이더: `scrapling/spiders/`
  - 유틸: `scrapling/engines/toolbelt/`

---

*이 문서는 Scrapling 학습 과정에서 정리한 참고 자료입니다. 필요에 따라 자유롭게 수정/확장하세요.*
