"""
Playwright 기반 API 엔드포인트 자동 발견 전략.

SPA (Nuxt/Next/Vue/React) 사이트는 HTML 이 빈 껍데기이고 JS 가 API 를 호출해서
채용공고를 가져온다. 이 전략은 헤드리스 Chromium 으로 그 API 요청을
스니핑해서 "채용 목록 API 같은 것" 을 점수화해 상위 후보를 반환한다.

결과는 "수동 어댑터를 위한 재료" 다 — 자동 크롤링은 하지 않는다.
사람이 점수 상위 후보를 보고 hardcoded_crawls.py 에 어댑터를 추가해야 한다.

트리거 조건:
    analyzer 가 이 전략을 부르기 전에 heuristic 이 SPA (SPA_NUXT/NEXT/VUE/REACT)
    로 판정한 경우에만 호출됨. (브라우저 띄우는 비용이 크므로)
"""

import json
import re
from typing import Optional
from urllib.parse import urlparse

from ..models import AnalysisResult, SiteType
from .base import AnalysisStrategy


# LLM 랭커에 보낼 후보 개수 (너무 많이 보내면 토큰 비용 ↑, 적으면 진짜를 놓침)
LLM_RANKER_POOL_SIZE = 10

LLM_RANKER_SYSTEM_PROMPT = """너는 채용공고 사이트에서 캡처된 내부 API 후보들 중
실제 "공고 리스트"를 반환하는 API 를 식별하는 분석기야.

제외해야 할 것:
- 메타데이터 / 필터 옵션 (직급/업종/복리후생 코드, 지역 코드 등)
- 트래킹 / 애널리틱스 / 광고 / 로그인 / 세션
- 상세 페이지 단건 조회 (배열 아닌 단일 객체)
- 추천/광고용 배너, 사이드바 위젯

선호 시그널:
- 응답이 "여러 개 공고" 를 담은 배열/페이지 (title/company/salary/location/date 같은
  실제 공고성 필드를 가진 객체들)
- 페이지네이션 필드 (total, page, size 등)
- 배열 길이가 보통 10~100개 수준

중요: 완벽한 매치가 없어도 **가장 공고 리스트에 가까워 보이는** 후보를 골라라.
가령 필드명이 한국어/축약형이거나 (jobTitle/compNm/giupNm/postSubject) 구조가 예상과
다르더라도, "여러 레코드" + "텍스트성 필드 여럿" 이면 공고 리스트 후보로 유효.
best_index=-1 은 정말로 **전부 명백한 메타데이터/트래킹** 일 때만 쓴다.

응답은 반드시 JSON 하나만. 마크다운 펜스나 설명 없이.
스키마: {"best_index": N, "reason": "한줄 근거"}
"""


def _build_ranker_user_message(candidates: list, exclude: Optional[list] = None) -> str:
    """LLM 에게 보낼 candidates 요약 문자열.

    exclude: 직전 시도에서 선택됐다가 validator 가 거부한 index 리스트.
             있으면 "이 index 들은 공고 API 가 아님이 확인됨 — 제외하고 다시 고르라" 를 프롬프트에 추가.
    """
    lines = ["아래 후보 중 공고 리스트 API 의 index 를 골라라.\n"]
    if exclude:
        lines.append(
            f"※ 직전 시도에서 고른 index {exclude} 는 실제 호출해봤을 때 필터옵션/코드테이블로 "
            f"판명됨. 이 index 는 다시 고르지 말고 다른 후보 중에서 선택하라."
        )
        lines.append("")
    for i, c in enumerate(candidates):
        shape = c["response_shape"]
        marker = " [제외됨]" if exclude and i in exclude else ""
        lines.append(f"[{i}]{marker} {c['method']} {c['url']}")
        lines.append(f"    score: {c.get('score')}")
        lines.append(f"    response_shape: {json.dumps(shape, ensure_ascii=False)[:500]}")
        lines.append(f"    sample: {c['body_snippet'][:300]}")
        lines.append("")
    return "\n".join(lines)


def llm_rank_candidates(
    pool: list[dict],
    api_key: str,
    model: str = "gpt-4o-mini",
    timeout: int = 30,
    exclude: Optional[list] = None,
) -> tuple[Optional[int], str]:
    """LLM 으로 pool 에서 진짜 공고 리스트 API 를 고른다 (재시도 시 외부에서도 호출 가능).

    exclude: 이전에 골랐다가 거부된 index 들. LLM 이 또 고르면 호출부에서 폐기.

    Returns: (index, reason)
        - (0..len-1, reason): LLM 이 고른 index
        - (-1, reason)       : LLM 이 명시적으로 "적절한 후보 없음"
        - (None, reason)     : 호출/파싱 실패
    """
    try:
        from openai import OpenAI

        client = OpenAI(api_key=api_key, timeout=timeout)
        response = client.chat.completions.create(
            model=model,
            max_tokens=256,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": LLM_RANKER_SYSTEM_PROMPT},
                {"role": "user", "content": _build_ranker_user_message(pool, exclude=exclude)},
            ],
        )
        raw = response.choices[0].message.content or ""
    except Exception as e:
        return None, f"LLM 호출 실패: {type(e).__name__}: {e}"

    parsed = _extract_json(raw)
    if parsed is None:
        return None, f"LLM 응답 JSON 파싱 실패: {raw[:200]!r}"

    try:
        idx = int(parsed.get("best_index", -1))
    except (TypeError, ValueError):
        return None, f"best_index 숫자 아님: {parsed!r}"

    reason = str(parsed.get("reason", ""))[:300]

    # -1 = 명시 거부 (호출부가 별도 처리)
    if idx < 0:
        return -1, f"LLM: 적절한 공고 API 없음 — {reason}"

    if idx >= len(pool):
        return None, f"LLM: 범위 초과 index={idx} (pool={len(pool)}) — {reason}"

    # exclude 에 있는데 또 고른 경우 — LLM 실수, 호출부가 재시도 중단 판단
    if exclude and idx in exclude:
        return None, f"LLM 이 이미 제외된 index={idx} 를 또 선택 — 재시도 한계 (reason={reason})"

    return idx, reason


def _extract_json(text: str) -> Optional[dict]:
    """LLM 응답에서 JSON 꺼내기 — response_format=json_object 덕에 보통 그대로 파싱됨."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```\s*$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def build_config_from_candidate(candidate: dict, url: str) -> dict:
    """pool 의 한 candidate dict 에서 api_discovered config 를 만든다.

    재시도 시 main.py 에서 사용 — 다른 후보 pick 으로 config 재생성할 때 쓴다.
    원래 PlaywrightDiscoveryStrategy.run 이 만드는 config 와 동일한 구조.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return {
        "platform": "api_discovered",
        "base_url": f"{parsed.scheme}://{parsed.netloc}",
        "api_endpoint": candidate["url"],
        "method": candidate["method"],
        "request_headers": candidate["request_headers"],
        "post_data": candidate.get("post_data"),  # POST body 원본 (문자열)
        "response_shape": candidate["response_shape"],
        "response_sample": candidate["body_snippet"],
        "selection_source": "llm_retry",
        "needs_manual_adapter": True,
    }


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 네트워크가 잠잠해진 후에도 lazy-load / 지연 XHR 를 잡기 위한 추가 대기 (ms)
# 알바몬/사람인 같이 SPA 가 필터 옵션 API 먼저 호출하고 실제 공고 API 는 스크롤 / 사용자
# 인터랙션 이후에 호출되는 경우 대응. 15초로 늘리고 추가로 아래 루프에서 스크롤 시도.
POST_LOAD_WAIT_MS = 15_000

# 응답 본문 최대 크기 — 이보다 크면 "목록 API 가 아닌 대용량 자원" 으로 보고 스킵
MAX_RESPONSE_BYTES = 500_000

# 최소 응답 크기 — 너무 작으면 빈 ping/track 요청
MIN_RESPONSE_BYTES = 50

# 페이지 네비게이션 타임아웃 (ms) — SPA 는 느릴 수 있어 넉넉히
NAVIGATION_TIMEOUT_MS = 30_000


# 점수 가중치
URL_POSITIVE_KEYWORDS = [
    "job", "vacancy", "career", "position", "recruit",
    "list", "search", "page-query", "pageQuery", "listing",
    "post", "article", "board",
]
URL_NEGATIVE_KEYWORDS = [
    "track", "analytic", "ads", "pixel", "beacon", "gtm",
    "google", "facebook", "doubleclick", "sentry",
    "hotjar", "mixpanel", "segment",
    "favicon", ".css", ".woff", ".ttf", ".png", ".jpg", ".svg",
    "login", "logout", "auth", "token", "session",
]


# 페이지네이션 시그니처 필드명 (응답에 있으면 목록 API 가능성 높음)
PAGINATION_KEYS = {
    "page", "pageno", "pageNo", "pageNum", "pageNumber",
    "total", "totalPage", "totalPages", "totalCount", "totalElements",
    "count", "size", "pageSize", "limit",
    "hasNext", "hasMore", "next", "nextPageToken", "cursor",
}


class PlaywrightDiscoveryStrategy(AnalysisStrategy):
    """
    헤드리스 Chromium 으로 SPA 내부 API 엔드포인트를 탐지한다.

    장점: 사람이 개발자도구 여는 작업을 자동화
    단점: 브라우저 실행 비용 (수 초), 안티봇 감지 가능성
    사용 시점: heuristic 이 SPA 로 판정했을 때만 (analyzer 가 라우팅)
    """

    def __init__(
        self,
        enabled: bool = True,
        post_load_wait_ms: int = POST_LOAD_WAIT_MS,
        navigation_timeout_ms: int = NAVIGATION_TIMEOUT_MS,
        top_n: int = 3,
        use_llm_ranker: bool = False,
        llm_api_key: Optional[str] = None,
        llm_model: str = "gpt-4o-mini",
        llm_timeout: int = 30,
    ):
        """
        post_load_wait_ms: load 이벤트 이후 추가 대기 시간 (지연 XHR 잡기용)
        navigation_timeout_ms: page.goto 타임아웃
        top_n: 리포트에 담을 상위 후보 개수
        use_llm_ranker: 규칙 점수화 상위 N개를 LLM 에게 보내 "진짜 공고 API" 재선별
        llm_api_key: OPENAI_API_KEY (use_llm_ranker=True 일 때 필요)
        llm_model: 랭커용 모델 (기본 gpt-4o-mini — 이 판단엔 충분)
        llm_timeout: LLM 호출 타임아웃 (초)
        """
        super().__init__(enabled=enabled, name="playwright_discovery")
        self.post_load_wait_ms = post_load_wait_ms
        self.navigation_timeout_ms = navigation_timeout_ms
        self.top_n = top_n
        self.use_llm_ranker = use_llm_ranker
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model
        self.llm_timeout = llm_timeout

    def analyze(self, url: str, html: str = None) -> Optional[AnalysisResult]:
        try:
            captured = self._capture_network(url)
        except Exception as e:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes=f"Playwright 실행 실패: {type(e).__name__}: {e}",
            )

        if not captured:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes="JSON 응답 XHR 을 하나도 캡처하지 못함 "
                      "(네트워크 지연 / 로그인 필요 / 안티봇 가능성)",
            )

        scored = [(self._score(c), c) for c in captured]
        scored.sort(key=lambda x: x[0], reverse=True)

        # LLM 랭커를 쓸 땐 풀을 넓게 — 규칙점수가 애매한 (URL 키워드 없는) 진짜 공고 API 도 포함.
        # score <= -3 (트래킹/광고/정적자원) 만 명확히 제외.
        # LLM 없을 땐 기존대로 score > 0 만 후보.
        if self.use_llm_ranker:
            filtered = [(s, c) for s, c in scored if s > -3]
        else:
            filtered = [(s, c) for s, c in scored if s > 0]

        if not filtered:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes=f"{len(captured)}개 JSON 응답 캡처했지만 채용 API 후보 없음 "
                      f"(힌트 키워드 부족 / 전부 트래킹성)",
            )

        # LLM 랭커가 보는 풀은 상위 N개. LLM 없으면 이 풀이 곧 최종 상위.
        pool_size = LLM_RANKER_POOL_SIZE if self.use_llm_ranker else self.top_n
        pool = [{"score": s, **c} for s, c in filtered[:pool_size]]

        llm_selected_index: Optional[int] = None
        llm_reason = ""
        llm_explicit_reject = False
        if self.use_llm_ranker and self.llm_api_key and len(pool) > 0:
            llm_selected_index, llm_reason = llm_rank_candidates(
                pool,
                api_key=self.llm_api_key,
                model=self.llm_model,
                timeout=self.llm_timeout,
            )
            if llm_selected_index == -1:
                # LLM 이 "전부 메타데이터/트래킹" 명시 판정 — 규칙점수 폴백 금지.
                # 환각 방어: 후보가 명백히 공고 API 아닌데도 score 로 억지 선택하는 사고 방지.
                llm_explicit_reject = True

        if llm_explicit_reject:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes=(
                    f"{len(captured)}개 JSON 응답 캡처 / {len(filtered)}개 score 통과했으나 "
                    f"LLM 판정: 모두 메타데이터/트래킹 — {llm_reason}"
                ),
            )

        if llm_selected_index is not None and 0 <= llm_selected_index < len(pool):
            best = pool[llm_selected_index]
            best_score = best["score"]
            selection_source = f"llm (idx={llm_selected_index})"
        else:
            # LLM 미사용 / 호출 실패 / 파싱 실패 → 규칙점수 1위
            best = pool[0]
            best_score = best["score"]
            selection_source = "rule_score"

        parsed = urlparse(url)

        config = {
            "platform": "api_discovered",
            "base_url": f"{parsed.scheme}://{parsed.netloc}",
            "api_endpoint": best["url"],
            "method": best["method"],
            "request_headers": best["request_headers"],
            "post_data": best.get("post_data"),  # POST body — LG·토스 등 대응
            "response_shape": best["response_shape"],
            "response_sample": best["body_snippet"],
            "selection_source": selection_source,
            "llm_reason": llm_reason,
            "all_candidates": [
                {
                    "url": c["url"],
                    "method": c["method"],
                    "score": c["score"],
                    "response_shape": c["response_shape"],
                }
                for c in pool[: self.top_n]
            ],
            # 전체 pool 을 내부용으로 보관 — main.py 의 validator 실패 재시도 루프에서 다른
            # 후보로 재선택할 때 필요. 언더스코어 prefix 는 "sites.json 에 저장하지 않는 내부 필드"
            # 의 관례 (sites_registry._api_analysis_to_source 가 명시 필드만 복사하므로 누락됨).
            "_ranker_pool": pool,
            "_ranker_selected_index": llm_selected_index,
            "needs_manual_adapter": True,
        }

        # confidence 는 최고점 기준 러프하게 — 7점 이상이면 0.8, 그 이하는 비례
        confidence = min(0.9, 0.3 + best_score * 0.07)

        notes = (
            f"API 후보 {len(filtered)}개 발견 (전체 캡처 {len(captured)}), "
            f"선택: {best['url']} (score={best_score}, source={selection_source})"
        )
        if llm_reason:
            notes += f" | LLM: {llm_reason}"

        return AnalysisResult(
            url=url,
            site_type=SiteType.API_DISCOVERED,
            confidence=confidence,
            config=config,
            strategy_name=self.name,
            notes=notes,
        )

    # --- 내부 헬퍼 ---

    def _capture_network(self, url: str) -> list[dict]:
        """
        Playwright 로 페이지 열고 JSON 응답들 수집.
        각 항목: {url, method, request_headers, body_snippet, response_shape, size}
        """
        import os
        from urllib.parse import urlparse
        from playwright.sync_api import sync_playwright

        captured: list[dict] = []

        launch_kwargs: dict = {"headless": True}
        proxy_url = os.environ.get("PLAYWRIGHT_PROXY") or os.environ.get("HTTPS_PROXY")
        if proxy_url:
            pu = urlparse(proxy_url)
            proxy_cfg = {"server": f"{pu.scheme}://{pu.hostname}:{pu.port}"}
            if pu.username:
                proxy_cfg["username"] = pu.username
            if pu.password:
                proxy_cfg["password"] = pu.password
            launch_kwargs["proxy"] = proxy_cfg
            print(f"      [playwright] proxy 사용: {pu.hostname}:{pu.port}")

        with sync_playwright() as p:
            browser = p.chromium.launch(**launch_kwargs)
            context = browser.new_context(user_agent=USER_AGENT, ignore_https_errors=True)
            page = context.new_page()

            def on_response(response):
                try:
                    ct = (response.headers.get("content-type") or "").lower()
                    if "json" not in ct:
                        return
                    if response.status != 200:
                        return

                    try:
                        body = response.body()
                    except Exception:
                        return

                    size = len(body)
                    if size < MIN_RESPONSE_BYTES or size > MAX_RESPONSE_BYTES:
                        return

                    text = body.decode("utf-8", errors="replace")
                    try:
                        parsed = json.loads(text)
                    except Exception:
                        return

                    # POST body (request payload) — 검색조건이 body 에 들어가는 API 대응.
                    # LG(retrieveJobNoticesList)·토스·우아한처럼 GET 405 / POST body 필수인 케이스.
                    post_data = None
                    try:
                        post_data = response.request.post_data
                    except Exception:
                        pass

                    captured.append({
                        "url": response.url,
                        "method": response.request.method,
                        "request_headers": dict(response.request.headers),
                        "post_data": post_data,  # POST body raw 문자열 (보통 JSON). GET 이면 None.
                        "body_snippet": text[:800],
                        "response_shape": self._infer_shape(parsed),
                        "size": size,
                    })
                except Exception:
                    pass  # 개별 응답 처리 실패는 조용히 스킵

            page.on("response", on_response)

            try:
                page.goto(
                    url,
                    wait_until="load",
                    timeout=self.navigation_timeout_ms,
                )
            except Exception:
                pass  # 일부 SPA 는 load 이벤트 안 쏴 — 그래도 캡처된 건 쓸 수 있음

            # 스크롤로 lazy-load 유도 — 알바몬·사람인 등 SPA 가 공고 API 를 초기 로드가
            # 아닌 viewport 진입 시에만 호출하는 경우 대응. 여러 단계 스크롤 + 각 단계
            # 대기로 네트워크 응답 캡처.
            try:
                for scroll_ratio in (0.3, 0.6, 0.9, 0.5, 1.0):
                    page.evaluate(
                        f"window.scrollTo(0, document.body.scrollHeight * {scroll_ratio})"
                    )
                    page.wait_for_timeout(1500)
            except Exception:
                pass

            # lazy-load / 지연 XHR 잡기용 추가 대기
            page.wait_for_timeout(self.post_load_wait_ms)

            browser.close()

        return captured

    def _infer_shape(self, obj, depth: int = 0, max_depth: int = 3) -> dict:
        """JSON 응답의 구조를 요약 (디버그/점수화용)."""
        if depth > max_depth:
            return {"truncated": True}

        if isinstance(obj, list):
            return {
                "type": "array",
                "length": len(obj),
                "first_item": (
                    self._infer_shape(obj[0], depth + 1, max_depth) if obj else None
                ),
            }

        if isinstance(obj, dict):
            result = {"type": "object", "keys": list(obj.keys())[:20]}

            if any(k in obj for k in PAGINATION_KEYS):
                result["has_pagination"] = True

            for k, v in obj.items():
                if isinstance(v, list) and len(v) > 0:
                    result["array_field"] = k
                    result["array_length"] = len(v)
                    result["array_first_item"] = self._infer_shape(v[0], depth + 1, max_depth)
                    break
                if isinstance(v, dict):
                    nested = self._infer_shape(v, depth + 1, max_depth)
                    if nested.get("array_field"):
                        result["nested_array_path"] = f"{k}.{nested['array_field']}"
                        result["array_length"] = nested.get("array_length", 0)
                        result["array_first_item"] = nested.get("array_first_item")
                        break

            return result

        return {"type": type(obj).__name__}

    def _score(self, candidate: dict) -> int:
        """
        목록 API 일 가능성 점수화. 높을수록 유력.
        (-∞ ~ +∞ 범위, 0 이하면 후보에서 제외)
        """
        score = 0
        url_lower = candidate["url"].lower()
        shape = candidate["response_shape"]

        for kw in URL_POSITIVE_KEYWORDS:
            if kw in url_lower:
                score += 2
        for kw in URL_NEGATIVE_KEYWORDS:
            if kw in url_lower:
                score -= 5

        # 응답에 배열이 있으면 +3, 페이지네이션 필드 있으면 +2
        if shape.get("array_field") or shape.get("nested_array_path") or shape.get("type") == "array":
            score += 3
        if shape.get("has_pagination"):
            score += 2

        array_len = shape.get("array_length") or (
            shape.get("length") if shape.get("type") == "array" else 0
        )
        if array_len and array_len >= 5:
            score += 2

        # 크기 — 중간 크기가 이상적
        size = candidate["size"]
        if 500 < size < 100_000:
            score += 1

        return score
