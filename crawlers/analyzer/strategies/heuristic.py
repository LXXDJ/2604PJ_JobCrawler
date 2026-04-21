"""
휴리스틱 기반 사이트 분석 전략

HTML 내용을 분석해서 사이트 타입을 판단하고
타입별로 크롤러 설정을 추출한다.

감지 가능한 타입:
- GNUBOARD : HTML에 g5_bbs_url 등 그누보드 전역변수 존재
- SPA_NUXT : __NUXT__ / _nuxt/ 경로 존재
- SPA_NEXT : __NEXT_DATA__ 존재
- STATIC_HTML : 위에 해당 없음
"""

import html as html_lib
import re
import time
import requests
from typing import Optional
from urllib.parse import parse_qs, urlparse
from ..models import AnalysisResult, SiteType
from .base import AnalysisStrategy


# 상세링크 ID 파라미터 후보 — 점수 동률일 때 tie-breaker 로만 사용.
# 보너스를 크게 주면 radiokorea 처럼 본문은 id= 이지만 footer/archive 영역의 소수 wr_id 에
# 넘어가므로, 기본은 "distinct numeric 값이 가장 많은 파라미터" 가 이긴다.
DETAIL_ID_PARAM_CANDIDATES = ("wr_id", "no", "id", "idx", "bno", "seq")


def _detect_detail_id_param(html: str) -> Optional[str]:
    """페이지의 앵커 href 들을 훑어 "numeric ID" 로 가장 잘 맞는 쿼리 파라미터를 추정.

    예) ppomppu — view.php?id=guin&no=9860 → 'no' 가 각 링크마다 다른 숫자
       radiokorea — jobs_ads_view.php?id=4177 → 'id'
       hanin — board.php?bo_table=Information&wr_id=58 → 'wr_id' (2자리 ID 도 허용)

    판정 규칙:
      - href 는 HTML 엔티티 디코드 (`&amp;` → `&`) 후 쿼리 파싱 —
        안 그러면 `&amp;wr_id=` 가 `amp;wr_id` 키로 잘못 파싱됨.
      - 각 파라미터별 "등장한 숫자값 집합" 수집 (2자리 이상)
      - 고유 숫자값 3개 미만은 후보 탈락 (nav/footer 배제)
      - 기본 순위는 distinct 값 개수. 같은 개수라면 DETAIL_ID_PARAM_CANDIDATES 내 순서 우대.
      - 후보 없으면 None — caller 가 기본값 폴백 (gnuboard 는 'wr_id')
    """
    hrefs = re.findall(r'href="([^"]+)"', html)
    param_values: dict[str, set] = {}
    for raw in hrefs:
        h = html_lib.unescape(raw)
        try:
            qs = parse_qs(urlparse(h).query)
        except Exception:
            continue
        for k, vs in qs.items():
            for v in vs:
                if v.isdigit() and len(v) >= 2:
                    param_values.setdefault(k, set()).add(v)

    scored = []
    for k, vs in param_values.items():
        if len(vs) < 3:
            continue
        # 1순위: distinct 개수. 2순위: 후보 리스트 내 우선순위(있으면).
        cand_rank = (
            len(DETAIL_ID_PARAM_CANDIDATES) - DETAIL_ID_PARAM_CANDIDATES.index(k)
            if k in DETAIL_ID_PARAM_CANDIDATES
            else 0
        )
        scored.append((len(vs), cand_rank, k))

    if not scored:
        return None
    scored.sort(reverse=True)
    return scored[0][2]


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class HeuristicStrategy(AnalysisStrategy):
    """
    규칙 기반 사이트 분석.

    장점: 빠르고 무료, 결정적
    단점: 알려진 패턴만 처리 가능
    """

    def __init__(
        self,
        enabled: bool = True,
        timeout: int = 30,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
    ):
        """
        timeout: 1회 요청의 타임아웃(초)
        max_retries: 실패 시 재시도 횟수
        retry_backoff: 재시도 간 대기 시간 배수 (1회: backoff, 2회: backoff*2, ...)
        """
        super().__init__(enabled=enabled, name="heuristic")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff

    def analyze(self, url: str, html: str = None) -> Optional[AnalysisResult]:
        if html is None:
            html, fetch_notes = self._fetch_html(url)
            if html is None:
                return AnalysisResult(
                    url=url,
                    site_type=SiteType.UNKNOWN,
                    confidence=0.0,
                    strategy_name=self.name,
                    notes=f"HTML 가져오기 실패: {fetch_notes}",
                )

        # 1) 사이트 타입 분류
        site_type, confidence, signatures = self._classify(html)

        # 2) 타입별 config 추출
        config = self._extract_config(url, html, site_type)

        return AnalysisResult(
            url=url,
            site_type=site_type,
            confidence=confidence,
            config=config,
            strategy_name=self.name,
            notes=f"signatures: {', '.join(signatures)}" if signatures else "",
        )

    # --- HTML 가져오기 (재시도 포함) ---

    def _fetch_html(self, url: str) -> tuple[Optional[str], str]:
        """
        HTML을 가져온다. 실패 시 max_retries만큼 재시도.
        느린 서버나 일시적 네트워크 오류를 견디도록 설계됨.

        Returns: (html, error_notes)
          성공: (html_string, "")
          실패: (None, "시도 N회 모두 실패: ...")
        """
        last_error = None

        # http_client.fetch 는 내부적으로 curl_cffi 로 Chrome 131 impersonate.
        # UA/헤더만 위장하는 requests 로 가면 사람인/하이브레인/잡플래닛 등에서
        # JA3 기반 봇차단에 403 당함 — impersonate 필수.
        try:
            from http_client import fetch
        except ImportError:
            from crawlers.http_client import fetch

        try:
            # cf_bypass_on_403: Cloudflare WAF 뒤 사이트(리멤버·자소설·슈퍼루키)
            # 대응 — 403 만나면 Playwright 로 cf_clearance 쿠키 워밍업 후 1회 재시도.
            # use_stealth_on_fail: curl_cffi 가 DNS/SSL/Timeout 등으로 완전 실패하면
            # scrapling StealthyFetcher 로 최후 폴백 — LG/현대차 careers 류 DNS 이슈,
            # 멀티잡 SSL, 캐치 SPA 등 대응.
            html = fetch(
                url,
                timeout=self.timeout,
                max_retries=self.max_retries,
                retry_backoff=self.retry_backoff,
                cf_bypass_on_403=True,
                use_stealth_on_fail=True,
            )
            return html, ""
        except requests.exceptions.HTTPError as e:
            # 4xx/5xx 는 http_client 가 즉시 raise.
            return None, f"HTTP {e.response.status_code}"
        except Exception as e:
            return None, f"시도 {self.max_retries}회 모두 실패: {type(e).__name__}: {e}"

    # --- 사이트 타입 분류 ---

    def _classify(self, html: str) -> tuple[SiteType, float, list[str]]:
        """HTML에서 플랫폼 시그니처를 찾아 타입 판단"""

        # 그누보드
        gnuboard_signatures = []
        if "g5_bbs_url" in html:
            gnuboard_signatures.append("g5_bbs_url")
        if "g5_bo_table" in html:
            gnuboard_signatures.append("g5_bo_table")
        if "/bbs/board.php" in html:
            gnuboard_signatures.append("/bbs/board.php")
        if gnuboard_signatures:
            confidence = 0.9 if len(gnuboard_signatures) >= 2 else 0.7
            return SiteType.GNUBOARD, confidence, gnuboard_signatures

        # Nuxt.js (Vue 기반 SSR)
        # 주의: 단순히 "__NUXT__" 문자열 매칭은 블로그 글/트래킹 JS 에서도 오탐될 수 있음.
        # 실제 할당 (`window.__NUXT__=`) 또는 번들 경로(`/_nuxt/`) 또는 n-head 속성 요구.
        nuxt_signatures = []
        if re.search(r"window\.__NUXT__\s*=", html):
            nuxt_signatures.append("window.__NUXT__=")
        if "/_nuxt/" in html:
            nuxt_signatures.append("/_nuxt/")
        if 'data-n-head' in html:
            nuxt_signatures.append("data-n-head")
        if nuxt_signatures:
            confidence = 0.9 if len(nuxt_signatures) >= 2 else 0.7
            return SiteType.SPA_NUXT, confidence, nuxt_signatures

        # Next.js (React 기반 SSR)
        # 주의: `__NEXT_DATA__` 단순 매칭은 문자열/주석에서도 나올 수 있어 script 태그
        # id 로 한정. `/_next/` 는 번들 경로라 위조 가능성 낮음.
        next_signatures = []
        if re.search(r'id\s*=\s*["\']__NEXT_DATA__["\']', html):
            next_signatures.append('id="__NEXT_DATA__"')
        if "/_next/" in html:
            next_signatures.append("/_next/")
        if next_signatures:
            confidence = 0.9 if len(next_signatures) >= 2 else 0.7
            return SiteType.SPA_NEXT, confidence, next_signatures

        # WordPress
        if "wp-content/" in html or 'name="generator" content="WordPress' in html:
            return SiteType.WORDPRESS, 0.8, ["wp-content/"]

        # 그 외 — 일단 정적 HTML로 간주
        return SiteType.STATIC_HTML, 0.3, []

    # --- 타입별 config 추출 ---

    def _extract_config(self, url: str, html: str, site_type: SiteType) -> dict:
        """사이트 타입별로 크롤러 설정을 추출한다."""

        if site_type == SiteType.GNUBOARD:
            return self._extract_gnuboard_config(url, html)

        if site_type == SiteType.SPA_NUXT:
            return self._extract_nuxt_config(url, html)

        # 다른 타입은 아직 미지원
        return {}

    def _extract_gnuboard_config(self, url: str, html: str) -> dict:
        """그누보드 사이트의 크롤러 설정 추출"""
        config = {"platform": "gnuboard"}

        # 전역변수에서 base_url, bo_table 추출
        base_match = re.search(r'g5_url\s*=\s*"([^"]+)"', html)
        if base_match:
            config["base_url"] = base_match.group(1)

        bbs_match = re.search(r'g5_bbs_url\s*=\s*"([^"]+)"', html)
        if bbs_match:
            config["bbs_url"] = bbs_match.group(1)

        table_match = re.search(r'g5_bo_table\s*=\s*"([^"]+)"', html)
        if table_match:
            config["board_table"] = table_match.group(1)

        # 게시글 목록 셀렉터 추론 (흔한 그누보드 테마)
        # 추후 개선: LLM이 HTML 보고 더 정확하게 판단
        # 클래스는 여러 개 붙어있을 수 있으므로 정규식으로 매칭
        if re.search(r'class="[^"]*\bna-table\b', html):
            config["theme"] = "nariya"
            config["selectors"] = {
                "list_rows": "ul.na-table > li",
                "subject_link": "a.na-subject",
                "author": "span.sv_member",
                "content": "div.view-content",
            }
            config["parse_mode"] = "sr_only"
        elif re.search(r'class="[^"]*\bfz_list\b', html):
            config["theme"] = "fz"
            config["selectors"] = {
                "list_rows": "ul.fz_list > li",
                "subject_link": "div.fz_subject > a",
                "author": "span.sv_member",
                "date": "div.fz_date",
                "hit": "div.fz_hit",
                "content": "#bo_v_con",
            }
            config["parse_mode"] = "direct"
        else:
            config["theme"] = "unknown"
            config["selectors"] = {}
            config["parse_mode"] = "unknown"

        # 상세링크 ID 파라미터 자동감지 — ppomppu(no)/radiokorea(id) 같은 변종 대응.
        # 감지 실패 시 converter 에서 'wr_id' 폴백.
        detected_id_param = _detect_detail_id_param(html)
        if detected_id_param:
            config["external_id_from_url_param"] = detected_id_param

        return config

    def _extract_nuxt_config(self, url: str, html: str) -> dict:
        """
        Nuxt.js 사이트의 config 추출.

        Nuxt 앱은 HTML에 데이터가 없고 JS가 API를 호출해서 동적 로딩한다.
        → API 엔드포인트를 찾아야 하는데, HTML에서 직접 찾기는 어려움.

        현재 휴리스틱으로 할 수 있는 것:
        - 이 사이트가 Nuxt임을 확인
        - API base URL 추측 (api.같은 서브도메인 탐색)
        - 실제 API 엔드포인트 발견은 Playwright 네트워크 캡처 필요 (별도 단계)
        """
        from urllib.parse import urlparse

        parsed = urlparse(url)
        domain = parsed.netloc

        # api.example.com 형태의 API 서브도메인을 후보로 제안
        # www. 프리픽스가 있으면 제거하고 api. 추가 (예: www.camhr.com → api.camhr.com)
        bare_domain = domain[4:] if domain.startswith("www.") else domain
        api_base_candidate = f"{parsed.scheme}://api.{bare_domain}"

        return {
            "platform": "nuxt",
            "base_url": f"{parsed.scheme}://{domain}",
            "api_base_candidate": api_base_candidate,
            "needs_playwright_discovery": True,  # Playwright로 API 찾아야 함
            "notes": "Nuxt SPA는 JS가 API를 호출하므로 네트워크 캡처 필요",
        }
