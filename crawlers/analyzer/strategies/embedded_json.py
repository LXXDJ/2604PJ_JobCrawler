"""
Embedded JSON 추출 전략.

SSR (Next.js / Nuxt) 사이트는 JS 렌더링 전에 HTML <script> 태그 안에
직렬화된 애플리케이션 상태를 심어둔다. 이 전략은 그 상태를 파싱해서
"공고 리스트 배열" 로 통하는 경로를 찾아 config 로 내놓는다.

Phase 2 범위:
    - <script id="__NEXT_DATA__"> (Next.js, 순수 JSON) 지원
    - window.__NUXT__=(factory function)(...) 형태는 Playwright 렌더 필요 → Phase 2.5 보류

검출 성공 시 SiteType.EMBEDDED_JSON_DISCOVERED + ExtractionMethod=embedded_json 로 분기.

트리거 조건:
    analyzer 가 heuristic 으로 SPA_NEXT/NUXT 로 판정한 뒤,
    playwright_discovery 가 공고 API 를 못 찾았을 때 (또는 playwright 비활성).
"""

import json
import re
from typing import Optional, Any
from urllib.parse import urlparse

import requests

from bs4 import BeautifulSoup

from ..models import AnalysisResult, SiteType
from .base import AnalysisStrategy


USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# state 내 배열 후보의 최소 길이 — 이거 미만이면 공고 리스트로 간주 안 함
MIN_ARRAY_LEN = 2

# 탐색 재귀 깊이 — 너무 깊이 들어가면 개별 아이템 내부 배열 (예: tag 리스트) 까지 포함됨
MAX_DEPTH = 8

# LLM 에 보낼 후보 최대 개수 (토큰 비용 관리)
MAX_CANDIDATES_FOR_LLM = 20

# 배열 첫 아이템에서 "텍스트성 필드" 판정 최소 길이
MIN_TEXT_FIELD_LEN = 3


# 지원하는 스크립트 후보 (순서대로 시도). format="json" 은 innerText 전체를 json.loads.
SCRIPT_CANDIDATES = [
    {"selector": "script#__NEXT_DATA__", "format": "json"},
    # 아래는 Phase 2.5 — 지금은 주석으로만 남김
    # {"selector": "script#__INITIAL_STATE__", "format": "json"},
]


LLM_PATH_SYSTEM_PROMPT = """너는 SSR (Next.js/Nuxt) 사이트 초기 상태 JSON 에서
'공고 리스트 배열' 이 있는 경로를 찾는 분석기야.

입력: 후보 배열들의 path/length/첫 아이템 키 요약.
출력은 반드시 JSON 하나만: {"item_path": "dot.notation.path", "reason": "한줄 근거"}

선호 신호:
- 길이 10+ 배열, 아이템이 title/company/position/jobName 등 텍스트 필드 다수 보유
- path 이름에 jobs/items/list/postings/results 같은 단어
제외:
- 메타데이터 (지역/직종/복리후생 코드 같은 옵션 리스트)
- 광고/배너/추천 위젯
- 필터/카테고리 네비게이션

item_path: "" (빈 문자열) 은 진짜 전부 메타/네비뿐일 때만 써.
부분 매치여도 가장 공고 리스트에 가까운 걸 골라.
"""


class EmbeddedJSONStrategy(AnalysisStrategy):
    """
    HTML <script> 안 SSR state 에서 공고 배열 경로를 자동으로 찾는다.

    장점: SSR 사이트는 첫 HTML 응답만으로 공고 데이터 추출 가능 (렌더 비용 X)
    단점: 상태 직렬화 포맷 (Next JSON vs Nuxt factory) 별 대응 코드 필요
    비용: 경로 선정에 LLM 1회 호출 (~$0.001)
    """

    def __init__(
        self,
        enabled: bool = True,
        timeout: int = 30,
        max_retries: int = 3,
        retry_backoff: float = 2.0,
        use_llm: bool = False,
        llm_api_key: Optional[str] = None,
        llm_model: str = "gpt-4o-mini",
        llm_timeout: int = 30,
    ):
        super().__init__(enabled=enabled, name="embedded_json")
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff = retry_backoff
        self.use_llm = use_llm
        self.llm_api_key = llm_api_key
        self.llm_model = llm_model
        self.llm_timeout = llm_timeout

    def analyze(self, url: str, html: str = None) -> Optional[AnalysisResult]:
        if html is None:
            html, err = self._fetch(url)
            if html is None:
                return AnalysisResult(
                    url=url,
                    site_type=SiteType.UNKNOWN,
                    confidence=0.0,
                    strategy_name=self.name,
                    notes=f"HTML 가져오기 실패: {err}",
                )

        soup = BeautifulSoup(html, "lxml")

        found = None
        for cand in SCRIPT_CANDIDATES:
            tag = soup.select_one(cand["selector"])
            if not tag or not tag.string:
                continue
            text = tag.string.strip()
            try:
                state = json.loads(text)
            except json.JSONDecodeError:
                continue
            found = {"selector": cand["selector"], "state": state}
            break

        if not found:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes="지원 포맷의 embedded JSON 스크립트 없음 "
                      "(현재: #__NEXT_DATA__ 만 지원, __NUXT__ factory form 은 Phase 2.5)",
            )

        candidates = self._find_array_paths(found["state"])
        if not candidates:
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes=f"state 파싱 성공 ({found['selector']}) 했지만 "
                      f"공고 배열 후보 없음 (len>={MIN_ARRAY_LEN} + 텍스트성 필드 있는 배열)",
            )

        # 길이/텍스트 필드 수로 초안 정렬 — LLM 미사용 시 1위가 최종
        candidates.sort(
            key=lambda c: (c["length"], len(c["text_fields"])),
            reverse=True,
        )

        best_path: Optional[str] = None
        reason = ""
        selection_source = ""
        llm_explicit_reject = False

        if self.use_llm and self.llm_api_key:
            picked, reason = self._llm_pick(candidates[:MAX_CANDIDATES_FOR_LLM])
            if picked:
                best_path = picked
                selection_source = "llm"
            elif picked == "":
                # LLM 이 명시적으로 "공고 리스트 없음" 판정 — 휴리스틱 폴백으로 덮어쓰지 않음.
                # 환각 방어: 메타데이터/필터 배열을 공고로 오인해 등록하는 사고 방지.
                llm_explicit_reject = True
            # picked is None (LLM 호출 실패) 는 폴백 허용

        if best_path is None and not llm_explicit_reject:
            best_path = candidates[0]["path"]
            if not selection_source:
                selection_source = "heuristic_top"
            if not reason:
                reason = (
                    f"휴리스틱 상위 (len={candidates[0]['length']}, "
                    f"text_fields={len(candidates[0]['text_fields'])})"
                )

        if best_path is None:
            # LLM 이 전부 메타/필터라 판단 → 정직한 실패
            return AnalysisResult(
                url=url,
                site_type=SiteType.UNKNOWN,
                confidence=0.0,
                strategy_name=self.name,
                notes=(
                    f"embedded state 내 배열 후보 {len(candidates)}개 발견했으나 "
                    f"LLM 판정: 모두 메타데이터/필터 — {reason}"
                ),
            )

        parsed = urlparse(url)
        first_cand = next((c for c in candidates if c["path"] == best_path), candidates[0])

        config = {
            "platform": "embedded_json",
            "base_url": f"{parsed.scheme}://{parsed.netloc}",
            "script_selector": found["selector"],
            "item_path": best_path,
            "item_length": first_cand["length"],
            "first_item_keys": first_cand["first_keys"],
            "first_item_sample": first_cand["first_sample"],
            "selection_source": selection_source,
            "llm_reason": reason,
            "all_candidates": [
                {"path": c["path"], "length": c["length"], "keys": c["first_keys"][:10]}
                for c in candidates[:10]
            ],
        }

        return AnalysisResult(
            url=url,
            site_type=SiteType.EMBEDDED_JSON_DISCOVERED,
            confidence=0.75,
            config=config,
            strategy_name=self.name,
            notes=(
                f"embedded JSON: {found['selector']} → "
                f"item_path={best_path} (length={first_cand['length']}, source={selection_source})"
            ),
        )

    # --- 내부 헬퍼 ---

    def _fetch(self, url: str) -> tuple[Optional[str], str]:
        """SSR 사이트의 HTML 가져오기 (재시도 포함)."""
        import time as _time
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                r = requests.get(
                    url,
                    headers={"User-Agent": USER_AGENT},
                    timeout=self.timeout,
                    verify=False,
                )
                r.raise_for_status()
                return r.text, ""
            except requests.exceptions.HTTPError as e:
                return None, f"HTTP {e.response.status_code}"
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
            if attempt < self.max_retries:
                _time.sleep(self.retry_backoff * attempt)
        return None, f"시도 {self.max_retries}회 실패: {last_error}"

    def _find_array_paths(self, obj: Any) -> list[dict]:
        """
        state JSON 을 재귀 순회하며 "공고 배열 후보" 들을 수집.

        후보 조건:
            - list 타입
            - 길이 >= MIN_ARRAY_LEN
            - 첫 아이템이 dict 이고, 텍스트성 필드를 최소 1개 보유

        배열 안쪽으로는 재귀하지 않음 (개별 아이템 내 sub-list 는 관심 없음).
        """
        out: list[dict] = []
        self._walk(obj, "", 0, out)
        return out

    def _walk(self, obj: Any, path: str, depth: int, out: list[dict]) -> None:
        if depth > MAX_DEPTH:
            return

        if isinstance(obj, list):
            if len(obj) >= MIN_ARRAY_LEN and isinstance(obj[0], dict):
                first = obj[0]
                text_fields = [
                    k for k, v in first.items()
                    if isinstance(v, str) and len(v) >= MIN_TEXT_FIELD_LEN
                ]
                if text_fields:
                    keys = list(first.keys())
                    out.append({
                        "path": path,
                        "length": len(obj),
                        "first_keys": keys[:20],
                        "text_fields": text_fields[:20],
                        "first_sample": {
                            k: self._truncate(first[k]) for k in keys[:10]
                        },
                    })
            # 배열 내부는 더 들어가지 않음
            return

        if isinstance(obj, dict):
            for k, v in obj.items():
                new_path = f"{path}.{k}" if path else k
                self._walk(v, new_path, depth + 1, out)

    def _truncate(self, v: Any, maxlen: int = 80) -> Any:
        """LLM 전송용 샘플 값 축약."""
        if isinstance(v, str):
            return v[:maxlen] + ("…" if len(v) > maxlen else "")
        if isinstance(v, (int, float, bool)) or v is None:
            return v
        if isinstance(v, list):
            return f"<list len={len(v)}>"
        if isinstance(v, dict):
            return f"<dict keys={list(v.keys())[:5]}>"
        return str(type(v).__name__)

    def _llm_pick(self, candidates: list[dict]) -> tuple[Optional[str], str]:
        """
        LLM 에게 후보 경로들을 보여주고 최적 경로 선택받기.

        Returns:
            (path, reason)       — 정상 선택
            ("", reason)         — LLM 이 명시적으로 "공고 리스트 없음" 판정 (폴백 금지)
            (None, reason)       — LLM 호출/파싱 실패 (폴백 허용)
        """
        try:
            from openai import OpenAI
        except Exception as e:
            return None, f"openai 모듈 로드 실패: {type(e).__name__}: {e}"

        try:
            client = OpenAI(api_key=self.llm_api_key, timeout=self.llm_timeout)
            lines = ["아래 후보 중 '공고 리스트 배열' 에 해당하는 item_path 를 골라라.\n"]
            for c in candidates:
                sample_str = json.dumps(c["first_sample"], ensure_ascii=False)[:400]
                lines.append(f"- path: {c['path']}")
                lines.append(f"  length: {c['length']}")
                lines.append(f"  text_fields: {c['text_fields']}")
                lines.append(f"  sample: {sample_str}")
                lines.append("")
            user_msg = "\n".join(lines)

            response = client.chat.completions.create(
                model=self.llm_model,
                max_tokens=256,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": LLM_PATH_SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
            )
            raw = (response.choices[0].message.content or "").strip()
        except Exception as e:
            return None, f"LLM 호출 실패: {type(e).__name__}: {e}"

        parsed = self._parse_json_lax(raw)
        if parsed is None:
            return None, f"LLM 응답 JSON 파싱 실패: {raw[:200]!r}"

        path = str(parsed.get("item_path") or "").strip()
        reason = str(parsed.get("reason", ""))[:300]
        if not path:
            # LLM 이 명시적으로 "없음" 반환 — 폴백 금지 sentinel
            return "", f"LLM: 적절한 경로 없음 — {reason}"

        # LLM 이 만든 path 가 실제 후보 중 하나인지 확인 (환각 방어)
        valid_paths = {c["path"] for c in candidates}
        if path not in valid_paths:
            return None, f"LLM 반환 path {path!r} 가 후보 리스트에 없음 (환각 의심)"

        return path, reason

    def _parse_json_lax(self, text: str) -> Optional[dict]:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*", "", text)
            text = re.sub(r"\s*```\s*$", "", text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
        return None
