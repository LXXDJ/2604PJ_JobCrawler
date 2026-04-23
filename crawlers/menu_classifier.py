"""
LLM 기반 메뉴 판별 — 메뉴 URL 에 들어가서 "구인공고 리스트 페이지" 인지 LLM 이 판단.

validator 만으로는 한계:
  - API 응답의 구조/필드명만 보므로, 커뮤니티 API 에 title/company 필드만 있어도 속음
  - 자소설닷컴 사례: "서류 떨어진 사람들" 같은 커뮤니티 글이 공고로 오인됨

LLM 은 페이지의 **시각적 맥락** + 캡처된 API 요약을 종합해서 판단:
  - visible 텍스트 샘플 (반복 패턴) — "회사명 + 직무 + 마감일" 같은 공고성 힌트
  - 캡처된 JSON API 중 어느 것이 실제 공고 배열 반환하는지 지목

흐름:
  classify_menu(url, menu_name, api_key, model)
    → {is_job_list, best_api_index, reason, captured_apis}

호출부는 is_job_list=False 면 해당 메뉴 skip. True 면 captured_apis[best_api_index]
를 가지고 sub_config 구성해서 validator/저장까지 진행.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse


MAX_SAMPLE_TEXTS = 40     # 페이지에서 뽑아 보낼 title-like 텍스트 상위 개수 (보조 신호)
MAX_API_SUMMARIES = 15    # LLM 에 보낼 API 후보 상위 개수 (size 기준)
MAX_PAGE_TEXT_CHARS = 12_000  # body.innerText 에서 LLM 에 넘길 문자수 상한 (토큰 비용 관리)
PAGE_LOAD_WAIT_MS = 15_000
NAVIGATION_TIMEOUT_MS = 30_000


CLASSIFIER_SYSTEM_PROMPT = """너는 웹페이지가 "구인공고 리스트 페이지" 인지 판단하는 분류기야.

판단 기준:
- "구인공고 리스트 페이지" = 여러 **채용공고** 를 목록으로 보여주는 페이지
  (회사명 + 직무/공고제목 + 마감일/지역/급여 같은 패턴 반복)
- 다음은 **아님**:
  - 커뮤니티 게시판 (후기/질문/자유글)
  - 필터/카테고리/지역 옵션 목록 (코드-라벨 매핑)
  - 회사 소개 / 서비스 설명 / 이벤트
  - 단일 공고 상세 페이지
  - 인재풀 (구직자 이력서 목록)

추가로, 판단이 true 라면 **coverage(범위)** 도 함께 판단해:
- "full"      → 사이트의 전체 공고 목록 (필터 없음, "전체/채용정보 홈/검색 메인" 류)
- "filtered"  → 특정 조건으로 **필터된 부분집합** (지역별/업직종별/자격별/근무조건별/기간별 등)
- "personal"  → 개인화 추천 (AI 추천/맞춤알바/스크랩/최근본알바) — 보통 로그인 필요
- "unknown"   → 판단 어려움

또한 캡처된 JSON API 중 **실제 공고 배열을 반환하는** API 의 index 를 지목해.
여러 개 있으면 가장 공고가 많은/정돈된 것으로. 공고 API 가 없으면 null.

응답: JSON 하나만. 마크다운 펜스 없이.
스키마: {"is_job_list": true|false, "coverage": "full|filtered|personal|unknown",
        "best_api_index": N|null, "reason": "한두 문장"}
"""


@dataclass
class ClassifyResult:
    is_job_list: bool
    coverage: str                   # "full" | "filtered" | "personal" | "unknown"
    best_api_index: Optional[int]
    reason: str
    captured_apis: list[dict]       # [{url, method, size, shape, request_headers, post_data, body_snippet}, ...]
    sample_texts: list[str]
    page_text: str                  # body/main 의 innerText (MAX_PAGE_TEXT_CHARS 로 잘린 상태)


def build_sub_config_from_captured(api: dict) -> dict:
    """classify_menu 가 캡처한 API 한 건으로 v2 sub_source config 구성.

    LLM 이 이미 공고 리스트로 판단한 API 이므로 analyzer 재실행 없이 validator 로 바로 넘긴다.
    item_path 는 빈 문자열로 시작 — validator 의 `_find_best_title_array` 자동 탐색이
    페이로드를 walk 해서 title-rich 배열 경로 찾아줌 (Gatsby/Strapi/GraphQL 대응).
    """
    from urllib.parse import urlparse, parse_qs

    endpoint_url = api.get("url", "")
    parsed = urlparse(endpoint_url)
    base_endpoint = f"{parsed.scheme}://{parsed.netloc}{parsed.path}" if parsed.scheme else endpoint_url
    list_params: dict = {}
    if parsed.query:
        try:
            list_params = {k: v[0] for k, v in parse_qs(parsed.query).items() if v}
        except Exception:
            list_params = {}

    # 헤더 정제 — HTTP/2 의사헤더 + 세션 헤더 제외
    headers: dict = {}
    for k, v in (api.get("request_headers") or {}).items():
        if not isinstance(k, str):
            continue
        if k.startswith(":") or k.lower() in ("cookie", "host", "content-length"):
            continue
        headers[k] = v

    method = (api.get("method") or "GET").upper()

    source = {
        "api_endpoint": base_endpoint,
        "method": method,
        "item_path": "",   # validator 자동 탐색
        "list_params": list_params,
    }
    if headers:
        source["request_headers"] = headers

    # POST body — 있으면 파싱 시도 (JSON). JSON 아니면 raw 문자열 저장.
    if method == "POST":
        post_data = api.get("post_data")
        if post_data:
            try:
                source["request_body"] = json.loads(post_data)
            except Exception:
                source["request_body"] = post_data

    return {
        "extraction_method": "api",
        "source": source,
        "pagination": {
            "type": "api_param",
            "param": "page",
            "start": int(list_params.get("page", 1)) if str(list_params.get("page", 1)).isdigit() else 1,
        },
    }


def _extract_json_blob(raw: str) -> Optional[dict]:
    """LLM 응답에서 JSON 오브젝트 추출 (마크다운 펜스 허용)."""
    raw = raw.strip()
    # 마크다운 펜스 제거
    raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.MULTILINE)
    try:
        return json.loads(raw)
    except Exception:
        # 본문 중간에 JSON 덩어리만 있을 수 있음
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:
                return None
    return None


def _infer_shape(obj, depth: int = 0, max_depth: int = 2) -> dict:
    """JSON 구조 요약 — LLM 프롬프트에 넣을 간략 버전."""
    if depth > max_depth:
        return {"truncated": True}
    if isinstance(obj, list):
        return {
            "type": "array",
            "length": len(obj),
            "first_item": _infer_shape(obj[0], depth + 1, max_depth) if obj else None,
        }
    if isinstance(obj, dict):
        keys = list(obj.keys())[:12]
        return {
            "type": "object",
            "keys": keys,
            "values_preview": {
                k: _infer_shape(obj[k], depth + 1, max_depth)
                for k in keys
                if isinstance(obj[k], (list, dict))
            },
        }
    t = type(obj).__name__
    if isinstance(obj, str):
        return {"type": "string", "sample": obj[:60]}
    return {"type": t}


def _capture_page(url: str):
    """Playwright 로 페이지 로드 → (captured_apis, sample_texts) 반환."""
    from playwright.sync_api import sync_playwright

    launch_kwargs = {"headless": True}
    proxy_url = os.environ.get("PLAYWRIGHT_PROXY") or os.environ.get("HTTPS_PROXY")
    if proxy_url:
        pu = urlparse(proxy_url)
        proxy_cfg = {"server": f"{pu.scheme}://{pu.hostname}:{pu.port}"}
        if pu.username:
            proxy_cfg["username"] = pu.username
        if pu.password:
            proxy_cfg["password"] = pu.password
        launch_kwargs["proxy"] = proxy_cfg

    captured: list[dict] = []
    sample_texts: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(ignore_https_errors=True)
        page = context.new_page()

        def on_response(response):
            try:
                ct = (response.headers.get("content-type") or "").lower()
                if "json" not in ct or response.status != 200:
                    return
                try:
                    body = response.body()
                except Exception:
                    return
                size = len(body)
                if size < 200 or size > 500_000:
                    return
                try:
                    parsed = json.loads(body.decode("utf-8", errors="replace"))
                except Exception:
                    return
                post_data = None
                try:
                    post_data = response.request.post_data
                except Exception:
                    pass
                captured.append({
                    "url": response.url,
                    "method": response.request.method,
                    "size": size,
                    "shape": _infer_shape(parsed),
                    "request_headers": dict(response.request.headers),
                    "post_data": post_data,
                    "body_snippet": body.decode("utf-8", errors="replace")[:500],
                })
            except Exception:
                pass

        page.on("response", on_response)

        try:
            page.goto(url, wait_until="load", timeout=NAVIGATION_TIMEOUT_MS)
        except Exception:
            pass

        # 스크롤로 lazy-load 유도
        try:
            for ratio in (0.3, 0.6, 0.9, 0.5, 1.0):
                page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {ratio})")
                page.wait_for_timeout(1200)
        except Exception:
            pass

        page.wait_for_timeout(PAGE_LOAD_WAIT_MS)

        # (a) 페이지 전체 가시 텍스트 수집 — main/content 영역 우선, 없으면 body.
        # 스크립트/스타일 제외된 innerText. LLM 에 컨텐츠 맥락 제공용.
        try:
            page_text_full = page.evaluate(
                """() => {
                    const sel = 'main, [role="main"], #content, #main, .content, .main, #container, #wrap, .wrap';
                    const el = document.querySelector(sel) || document.body;
                    if (!el) return '';
                    return (el.innerText || '').replace(/\\s+/g, ' ').trim();
                }"""
            ) or ''
        except Exception:
            page_text_full = ''

        # (b) 페이지 반복 텍스트 샘플 — 보조 신호. 제목 같은 반복 패턴을 뚜렷이 표시.
        # h2~h4/li/a 태그 중 6~120자 길이 텍스트 상위 MAX_SAMPLE_TEXTS 개.
        try:
            sample_texts = page.evaluate(
                """(maxCount) => {
                    const nodes = document.querySelectorAll('h2,h3,h4,li,a,article,[class*="title"],[class*="subject"]');
                    const seen = new Set();
                    const out = [];
                    for (const n of nodes) {
                        const t = (n.innerText || '').replace(/\\s+/g, ' ').trim();
                        if (t.length < 6 || t.length > 120) continue;
                        if (seen.has(t)) continue;
                        seen.add(t);
                        out.push(t);
                        if (out.length >= maxCount) break;
                    }
                    return out;
                }""",
                MAX_SAMPLE_TEXTS,
            ) or []
        except Exception:
            sample_texts = []

        browser.close()

    # === curl_cffi 보조 — Playwright 가 차단/빈 페이지 받았을 때 보강 ===
    # 하이브레인 같이 Playwright 헤드리스에 에러 페이지 주지만 curl_cffi(Chrome impersonate)
    # 로는 정상 HTML 받는 사이트 대응. page_text 가 너무 짧거나 "Access Denied" 같은
    # 차단 표시 있으면 curl 로 HTML 받아 BeautifulSoup 파싱.
    block_markers = (
        "access denied", "cloudflare", "blocked", "요청을 처리할 수 없", "페이지를 찾을 수 없",
        "error 403", "error 429", "접근이 차단", "suspicious activity",
    )
    needs_fallback = (
        len(page_text_full) < 500
        or any(m in page_text_full.lower() for m in block_markers)
    )
    if needs_fallback:
        try:
            try:
                from http_client import fetch
            except ImportError:
                from crawlers.http_client import fetch
            html = fetch(url, timeout=30, max_retries=1)
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "lxml")
            for s in soup(["script", "style", "noscript"]):
                s.decompose()
            main_el = soup.find(["main"]) or soup.find(attrs={"role": "main"})
            el = main_el or soup.body or soup
            curl_text = el.get_text(" ", strip=True) if el else ""
            if len(curl_text) > len(page_text_full):
                print(f"      [classify] curl_cffi fallback — page_text {len(page_text_full)} → {len(curl_text)}자")
                page_text_full = curl_text
            # sample_texts 보강
            curl_samples = []
            for tag in soup.select('h2,h3,h4,li,a,article,[class*="title"],[class*="subject"]'):
                t = (tag.get_text(" ", strip=True) or "").strip()
                if 6 <= len(t) <= 120 and t not in curl_samples and t not in sample_texts:
                    curl_samples.append(t)
                if len(sample_texts) + len(curl_samples) >= MAX_SAMPLE_TEXTS:
                    break
            if curl_samples:
                sample_texts.extend(curl_samples)
        except Exception as e:
            print(f"      [classify] curl_cffi fallback 실패: {type(e).__name__}: {str(e)[:100]}")

    # 텍스트 길이 상한 적용 — 토큰 비용 관리
    if len(page_text_full) > MAX_PAGE_TEXT_CHARS:
        page_text_full = page_text_full[:MAX_PAGE_TEXT_CHARS] + " …[truncated]"

    # size 내림차순으로 상위 후보 선택 (공고 리스트는 보통 큰 페이로드)
    captured.sort(key=lambda c: c["size"], reverse=True)
    return captured, sample_texts, page_text_full


def _build_user_message(url: str, menu_name: str, apis: list[dict],
                         texts: list[str], page_text: str) -> str:
    lines = [
        f"메뉴 이름: {menu_name!r}",
        f"URL: {url}",
        "",
        f"[페이지 본문 텍스트] (최대 {MAX_PAGE_TEXT_CHARS}자, main/body innerText)",
        page_text if page_text else "(페이지 텍스트 수집 실패)",
        "",
        "[반복 감지된 제목성 텍스트 샘플] (h2/h3/li/a 등에서 뽑음 — 공고 제목일 수도 아닐 수도):",
    ]
    for t in texts[:MAX_SAMPLE_TEXTS]:
        lines.append(f"  - {t}")
    lines.append("")
    lines.append(f"[페이지 로드 중 캡처된 JSON API 들] (최대 {MAX_API_SUMMARIES}개, size 내림차순):")
    for i, api in enumerate(apis[:MAX_API_SUMMARIES]):
        shape = api.get("shape") or {}
        shape_str = json.dumps(shape, ensure_ascii=False)[:300]
        lines.append(
            f"  [{i}] {api['method']} {api['url'][:120]} — {api['size']}B, shape={shape_str}"
        )
    return "\n".join(lines)


def classify_menu(url: str, menu_name: str, api_key: str,
                   model: str = "gpt-4o-mini", timeout: int = 60) -> ClassifyResult:
    """메뉴 URL 에 들어가서 LLM 에 "구인공고 리스트 페이지?" 질문."""
    captured, texts, page_text = _capture_page(url)

    if not api_key:
        # LLM 못 쓰면 캡처만 반환 (호출부가 fallback 로직 쓸 수도)
        return ClassifyResult(
            is_job_list=False,
            coverage="unknown",
            best_api_index=None,
            reason="LLM_API_KEY 없음 — 판별 생략",
            captured_apis=captured,
            sample_texts=texts,
            page_text=page_text,
        )

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, timeout=timeout)
        response = client.chat.completions.create(
            model=model,
            max_tokens=512,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": _build_user_message(url, menu_name, captured, texts, page_text)},
            ],
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        return ClassifyResult(
            is_job_list=False,
            coverage="unknown",
            best_api_index=None,
            reason=f"LLM 호출 실패: {type(e).__name__}: {e}",
            captured_apis=captured,
            sample_texts=texts,
            page_text=page_text,
        )

    parsed = _extract_json_blob(raw)
    if not isinstance(parsed, dict):
        return ClassifyResult(
            is_job_list=False,
            coverage="unknown",
            best_api_index=None,
            reason=f"LLM 응답 JSON 파싱 실패: {raw[:200]!r}",
            captured_apis=captured,
            sample_texts=texts,
            page_text=page_text,
        )

    is_job = bool(parsed.get("is_job_list", False))
    coverage = str(parsed.get("coverage", "unknown")).lower().strip()
    if coverage not in ("full", "filtered", "personal", "unknown"):
        coverage = "unknown"
    best_idx = parsed.get("best_api_index")
    if isinstance(best_idx, (int, float)):
        best_idx = int(best_idx)
        if best_idx < 0 or best_idx >= len(captured):
            best_idx = None
    else:
        best_idx = None
    reason = str(parsed.get("reason", ""))[:300]

    return ClassifyResult(
        is_job_list=is_job,
        coverage=coverage,
        best_api_index=best_idx,
        reason=reason,
        captured_apis=captured,
        sample_texts=texts,
        page_text=page_text,
    )
