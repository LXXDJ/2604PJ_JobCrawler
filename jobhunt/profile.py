"""내 타깃 프로필 — 1차 키워드 필터 규칙 + LLM 적합도 판단용 프로필 텍스트.

필터 규칙은 여기 코드에, 개인 경력 요약은 private/profile.md (gitignore) 에 둔다.
private/profile.md 가 없으면 profile.example.md 를 쓴다.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "private" / "profile.md"
EXAMPLE_PATH = Path(__file__).resolve().parent / "profile.example.md"

# 원티드 직무 카테고리 — 1634 = 머신러닝 엔지니어 (AI 직무 대부분이 여기 태깅됨)
WANTED_AI_CATEGORY = 1634

# 제목에 이 중 하나라도 있으면 후보. 영문 약어는 앞뒤가 영문자가 아닐 때만 (MAIN, EMAIL 오탐 방지)
_TITLE_KEYWORDS = [
    r"(?<![A-Za-z])(AI|ML|LLM|RAG|NLP|AX|GenAI|MLOps|LLMOps)(?![A-Za-z])",
    r"인공지능|머신\s?러닝|딥\s?러닝|생성형|자연어|챗봇|에이전트",
    r"(?i:agent|machine learning|forward deployed|prompt)",
]
TITLE_RE = re.compile("|".join(f"(?:{p})" for p in _TITLE_KEYWORDS))

# 제목에 있으면 제외 (직무 자체가 개발이 아님)
EXCLUDE_RE = re.compile(r"(?i)인턴|영업|세일즈|sales|마케팅|디자이너|강사|튜터|tutor|데이터\s?라벨|어노테이션")

# 연차 범위 — 2027.09 기준 AI 2년 + FE 5년. 하한이 너무 높거나 상한이 너무 낮은 공고는 제외
MAX_YEARS_FROM = 7      # "8년 이상" 부터는 제외
MIN_YEARS_TO = 2        # "0~1년" 신입 전용은 제외


def match(title: str, category_ids: list[int], years_from: int | None, years_to: int | None) -> tuple[bool, str]:
    """1차 필터. (통과여부, 사유) — LLM 호출량을 줄이기 위한 값싼 필터."""
    title = title or ""
    if EXCLUDE_RE.search(title):
        return False, "exclude_title"
    if years_from is not None and years_from > MAX_YEARS_FROM:
        return False, f"years_from>{MAX_YEARS_FROM}"
    if years_to is not None and years_to < MIN_YEARS_TO:
        return False, f"years_to<{MIN_YEARS_TO}"
    if WANTED_AI_CATEGORY in category_ids:
        return True, "category:ml"
    m = TITLE_RE.search(title)
    if m:
        return True, f"title:{m.group(0)}"
    return False, "no_keyword"


def profile_text() -> str:
    path = PROFILE_PATH if PROFILE_PATH.exists() else EXAMPLE_PATH
    return path.read_text(encoding="utf-8")
