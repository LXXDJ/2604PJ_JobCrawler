"""메뉴 후보 URL → LLM 분류 (full / filtered / personal / unknown).

라벨 정의:
  full      : 사이트의 모든 (또는 대다수) 채용 공고를 보여주는 리스트 페이지
              → sources 에 등록 대상
  filtered  : 특정 조건만 보여주는 리스트 (예: "신입만", "디자인 직군만")
              full 이 따로 있으면 배제 (full에 포함됨)
              full 없으면 여러 filtered 를 합쳐 등록
  personal  : 로그인/맞춤 공고 (개인화). 전수 수집 불가 → 배제
  unknown   : 채용 페이지 같지만 list 인지 detail 인지/판단 불가
              (개별 공고 상세, 기업용 등록 폼, 검색 입력 페이지 등 포함)
              → 배제

LLM 호출 비용/속도 때문에 한 번에 여러 후보를 묶어서 batch 처리.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Optional

from ..fetchers.static import fetch
from ..infra.config import LLM_MODEL, OPENAI_API_KEY


LABELS = ("full", "filtered", "personal", "unknown")


@dataclass
class ClassifyInput:
    url: str
    text: str = ""
    snippet: str = ""  # 페이지 일부 (있으면 정확도 ↑)


@dataclass
class ClassifyResult:
    url: str
    label: str
    reason: str = ""


SYSTEM_PROMPT = """\
당신은 한국 채용 사이트 메뉴를 분류하는 전문가입니다.
목표: 사이트의 "모든 공고" 를 누락 없이 수집하기 위해 등록 가능한 모든 리스트
페이지를 식별하는 것입니다.

각 URL 이 다음 4가지 중 어디에 해당하는지 판별하세요:

- "full"      : 사이트의 채용 공고 전체(혹은 대부분)를 한 화면에서 볼 수 있는
                통합 리스트. 페이지네이션이 있어도 됨.
                예: "전체 채용", "채용공고 목록", "All Jobs"

- "filtered"  : 특정 조건/카테고리만 보여주는 리스트 페이지.
                ※ 매우 중요 — 다음은 모두 filtered 로 분류:
                  · 직군별: "개발", "디자인", "영업/마케팅", "기획" 등
                  · 지역별: "서울", "경기", "해외" 등
                  · 고용형태별: "신입", "경력", "인턴", "정규직" 등
                  · 사업부/회사별: "삼성전자", "○○사업부" 등
                  · 부문별: "국내채용", "해외채용" 등
                filtered 는 등록 대상입니다 (full 과 합쳐 누락 없이 수집).

- "personal"  : 로그인 후 개인에 따라 다른 공고가 노출되는 페이지
                (맞춤 추천, 스크랩, 지원내역 등). 전수 수집 불가 → 제외.

- "unknown"   : 위 어디에도 속하지 않는 페이지.
                예: 개별 공고 상세 (/view/12345), 등록폼,
                    검색 입력창 (조건 필터 UI 만 있고 결과 없음),
                    회사소개/뉴스/이벤트, 판단 불가.

판단 근거:
  - URL path / query 패턴
  - 링크 텍스트 / 메뉴 라벨
  - 본문 일부 (snippet, 있을 때만)

규칙:
  - 카테고리/직군/지역/유형별 페이지는 무조건 filtered 로 (unknown 으로 보내면 안 됨)
  - 검색 결과 페이지처럼 보여도 카테고리 단위면 filtered
  - 단, 검색어 입력 폼만 있고 결과가 없는 페이지는 unknown

출력 형식 (엄격):
  - 반드시 JSON 객체 1개로 응답하며, 최상위 키는 "results" 입니다.
  - "results" 의 값은 입력으로 주어진 모든 후보(N개)에 대한 결과를 담은 배열입니다.
    입력 N개면 결과도 정확히 N개여야 하며, 각 입력의 url 은 결과에 정확히 같은 문자열로 포함되어야 합니다.
  - 다른 텍스트, 코드블록, 설명을 절대 포함하지 마세요.

예:
{
  "results": [
    {"url": "<입력 그대로>", "label": "full|filtered|personal|unknown", "reason": "..."},
    ...
  ]
}
"""


def _build_user_prompt(items: list[ClassifyInput]) -> str:
    blocks = []
    for i, it in enumerate(items, 1):
        block = f"[{i}] URL: {it.url}\n     TEXT: {it.text[:120]}"
        if it.snippet:
            block += f"\n     SNIPPET: {it.snippet[:400]}"
        blocks.append(block)
    return "다음 후보들을 분류하세요.\n\n" + "\n\n".join(blocks)


def _call_llm(items: list[ClassifyInput]) -> list[ClassifyResult]:
    if not OPENAI_API_KEY:
        return [ClassifyResult(url=it.url, label="unknown",
                               reason="OPENAI_API_KEY missing") for it in items]

    try:
        from openai import OpenAI
    except ImportError:
        return [ClassifyResult(url=it.url, label="unknown",
                               reason="openai package not installed") for it in items]

    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=LLM_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _build_user_prompt(items)},
        ],
        temperature=0,
        response_format={"type": "json_object"},
    )
    content = resp.choices[0].message.content or "{}"

    # LLM 이 반환할 수 있는 모양:
    #   1) [{"url":..,"label":..}, ...]              ← 배열 (이상)
    #   2) {"results": [...]}                        ← 래핑된 배열
    #   3) {"url":..,"label":..}                     ← 단일 dict (1개만 분류)
    #   4) {"<url>": {"label":..}, ...}              ← url 키 매핑
    parsed = json.loads(content)

    if isinstance(parsed, dict):
        # case 2: 안에 list 가 있으면 그걸 사용
        list_inside = next((v for v in parsed.values() if isinstance(v, list)), None)
        if list_inside is not None:
            parsed = list_inside
        # case 3: 단일 entry (url 키 보유)
        elif "url" in parsed and "label" in parsed:
            parsed = [parsed]
        # case 4: url 을 키로 쓴 dict 매핑
        elif all(isinstance(v, dict) for v in parsed.values()):
            parsed = [{"url": k, **v} for k, v in parsed.items()]

    by_url: dict[str, dict] = {}
    if isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict) and "url" in entry:
                by_url[entry["url"]] = entry

    out = []
    for it in items:
        e = by_url.get(it.url, {})
        label = e.get("label", "unknown")
        if label not in LABELS:
            label = "unknown"
        out.append(ClassifyResult(url=it.url, label=label, reason=e.get("reason", "")))
    return out


def _fetch_snippet(url: str, *, max_chars: int = 600) -> str:
    """페이지 일부 가져와 LLM 컨텍스트로 사용. 실패해도 무방."""
    r = fetch(url, timeout=10)
    if not r.ok:
        return ""
    # HTML 태그 일부 제거 (간단 버전 — bs4 부담 없이)
    import re
    txt = re.sub(r"<script[\s\S]*?</script>", " ", r.text, flags=re.I)
    txt = re.sub(r"<style[\s\S]*?</style>", " ", txt, flags=re.I)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:max_chars]


def classify_batch(
    items: list[ClassifyInput],
    *,
    batch_size: int = 20,
    fetch_snippet: bool = False,
) -> list[ClassifyResult]:
    if fetch_snippet:
        for it in items:
            if not it.snippet:
                it.snippet = _fetch_snippet(it.url)

    results: list[ClassifyResult] = []
    for i in range(0, len(items), batch_size):
        batch = items[i : i + batch_size]
        try:
            results.extend(_call_llm(batch))
        except Exception as e:  # noqa: BLE001
            for it in batch:
                results.append(ClassifyResult(url=it.url, label="unknown",
                                              reason=f"llm_error: {type(e).__name__}: {e}"))
    return results
