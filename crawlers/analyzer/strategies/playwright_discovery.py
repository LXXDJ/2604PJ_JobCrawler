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


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 네트워크가 잠잠해진 후에도 lazy-load / 지연 XHR 를 잡기 위한 추가 대기 (ms)
POST_LOAD_WAIT_MS = 4000

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
    ):
        """
        post_load_wait_ms: load 이벤트 이후 추가 대기 시간 (지연 XHR 잡기용)
        navigation_timeout_ms: page.goto 타임아웃
        top_n: 리포트에 담을 상위 후보 개수
        """
        super().__init__(enabled=enabled, name="playwright_discovery")
        self.post_load_wait_ms = post_load_wait_ms
        self.navigation_timeout_ms = navigation_timeout_ms
        self.top_n = top_n

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
        scored = [(s, c) for s, c in scored if s > 0]
        scored.sort(key=lambda x: x[0], reverse=True)

        if not scored:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes=f"{len(captured)}개 JSON 응답 캡처했지만 채용 API 점수 0 이하 "
                      f"(힌트 키워드 부족)",
            )

        top = [c for _, c in scored[: self.top_n]]
        best = top[0]
        best_score = scored[0][0]
        parsed = urlparse(url)

        config = {
            "platform": "api_discovered",
            "base_url": f"{parsed.scheme}://{parsed.netloc}",
            "api_endpoint": best["url"],
            "method": best["method"],
            "request_headers": best["request_headers"],
            "response_shape": best["response_shape"],
            "response_sample": best["body_snippet"],
            "all_candidates": [
                {
                    "url": c["url"],
                    "method": c["method"],
                    "score": s,
                    "response_shape": c["response_shape"],
                }
                for s, c in scored[: self.top_n]
            ],
            "needs_manual_adapter": True,
        }

        # confidence 는 최고점 기준 러프하게 — 7점 이상이면 0.8, 그 이하는 비례
        confidence = min(0.9, 0.3 + best_score * 0.07)

        return AnalysisResult(
            url=url,
            site_type=SiteType.API_DISCOVERED,
            confidence=confidence,
            config=config,
            strategy_name=self.name,
            notes=f"API 후보 {len(scored)}개 발견, 최상위: {best['url']} (score={best_score})",
        )

    # --- 내부 헬퍼 ---

    def _capture_network(self, url: str) -> list[dict]:
        """
        Playwright 로 페이지 열고 JSON 응답들 수집.
        각 항목: {url, method, request_headers, body_snippet, response_shape, size}
        """
        from playwright.sync_api import sync_playwright

        captured: list[dict] = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
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

                    captured.append({
                        "url": response.url,
                        "method": response.request.method,
                        "request_headers": dict(response.request.headers),
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
