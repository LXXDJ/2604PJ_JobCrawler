"""내 타깃 프로필 — 1차 키워드 필터 규칙 + LLM 적합도 판단용 프로필 텍스트.

필터 규칙은 여기 코드에, 개인 경력 요약은 private/profile.md (gitignore) 에 둔다.
private/profile.md 가 없으면 profile.example.md 를 쓴다.
"## 보유 기술" 섹션은 LLM 에 안 보내고 코드 매칭(fit.py)에만 쓴다.
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROFILE_PATH = ROOT / "private" / "profile.md"
EXAMPLE_PATH = Path(__file__).resolve().parent / "profile.example.md"

# 키워드 검색 방식 사이트(사람인·잡코리아) 검색어. 본문까지 검색되므로 넓게 걸고 제목 필터로 좁힌다.
# "인공지능" 은 사람인 1.5만건이라 제외 (2026-11-03 건수 확인)
SEARCH_KEYWORDS = ["LLM", "RAG", "AI Agent", "에이전트", "생성형 AI", "AI 엔지니어", "AI 개발자",
                   "머신러닝", "딥러닝", "NLP", "프롬프트", "AX"]

# 제목에 이 중 하나라도 있으면 후보. 영문 약어는 앞뒤가 영문자가 아닐 때만 (MAIN, EMAIL 오탐 방지)
_TITLE_KEYWORDS = [
    r"(?<![A-Za-z])(AI|ML|LLM|RAG|NLP|AX|GenAI|MLOps|LLMOps)(?![A-Za-z])",
    r"인공지능|머신\s?러닝|딥\s?러닝|생성형|자연어|챗봇|에이전트",
    r"(?i:agent|machine learning|forward deployed|prompt)",
]
TITLE_RE = re.compile("|".join(f"(?:{p})" for p in _TITLE_KEYWORDS))

# 제목에 있으면 제외 (직무 자체가 개발이 아님)
EXCLUDE_RE = re.compile(r"(?i)인턴|영업|세일즈|sales|마케팅|마케터|디자이너|강사|튜터|tutor|데이터\s?라벨|어노테이션"
                        r"|프리랜서|아르바이트|알바")

# 연차 범위 — 2027.09 기준 AI 2년 + FE 5년. 하한이 너무 높거나 상한이 너무 낮은 공고는 제외
MAX_YEARS_FROM = 7      # "8년 이상" 부터는 제외
MIN_YEARS_TO = 2        # "0~1년" 신입 전용은 제외


def match(title: str, ai_category: bool, years_from: int | None, years_to: int | None) -> tuple[bool, str]:
    """1차 필터. (통과여부, 사유) — LLM 호출량을 줄이기 위한 값싼 필터.
    ai_category = 사이트 직무 분류상 AI/ML 직무 (원티드 1634, 잡코리아 AI/ML엔지니어 등)."""
    title = title or ""
    if EXCLUDE_RE.search(title):
        return False, "exclude_title"
    if years_from is not None and years_from > MAX_YEARS_FROM:
        return False, f"years_from>{MAX_YEARS_FROM}"
    if years_to is not None and years_to < MIN_YEARS_TO:
        return False, f"years_to<{MIN_YEARS_TO}"
    if ai_category:
        return True, "category:ml"
    m = TITLE_RE.search(title)
    if m:
        return True, f"title:{m.group(0)}"
    return False, "no_keyword"


# 이 제목의 섹션은 코드만 쓰고 LLM 에는 보내지 않는다 → 고쳐도 LLM 재분석 없이 바로 반영
SKILLS_HEADING = "## 보유 기술"     # fit.py 기술 매칭
SALARY_HEADING = "## 연봉"          # dashboard.py 연봉 점수 (협상 후 숫자만 바꾸면 됨)
_CODE_ONLY = (SKILLS_HEADING, SALARY_HEADING)


def _raw() -> str:
    path = PROFILE_PATH if PROFILE_PATH.exists() else EXAMPLE_PATH
    return path.read_text(encoding="utf-8")


def _sections() -> tuple[str, dict[str, str]]:
    """(LLM 용 텍스트, {코드 전용 섹션 제목: 본문})."""
    llm_parts: list[str] = []
    code: dict[str, list[str]] = {}
    cur = None
    for line in _raw().splitlines():
        if line.startswith("## "):
            cur = next((h for h in _CODE_ONLY if line.startswith(h)), None)
            if cur:
                code[cur] = []
                continue
        (code[cur] if cur else llm_parts).append(line)
    return "\n".join(llm_parts).strip() + "\n", {h: "\n".join(v) for h, v in code.items()}


def _split() -> tuple[str, str]:
    """(LLM 용 텍스트, 보유 기술 섹션 본문)."""
    text, code = _sections()
    return text, code.get(SKILLS_HEADING, "")


def current_salary() -> int | None:
    """연봉 섹션의 "- 현재: 4,200만원" → 4200 (만원). 없으면 None (연봉 점수 안 씀)."""
    m = re.search(r"현재\s*:\s*([\d,]+)\s*만", _sections()[1].get(SALARY_HEADING, ""))
    return int(m.group(1).replace(",", "")) if m else None


def profile_text() -> str:
    return _split()[0]


def profile_hash() -> str:
    """LLM 이 보는 프로필 버전 — 이게 바뀐 공고만 재분석 대상 (보유 기술 섹션 수정은 영향 없음)."""
    return hashlib.sha1(profile_text().encode("utf-8")).hexdigest()[:12]


def skills_text() -> str:
    return _split()[1].strip()


def skills_hash() -> str:
    """보유 기술 섹션 버전 — 바뀌면 기술 표기 분류표 전체 재분류."""
    return hashlib.sha1(skills_text().encode("utf-8")).hexdigest()[:12]


def my_skills() -> tuple[list[str], list[str]]:
    """보유 기술 섹션의 "- 보유: a, b" / "- 일부: c, d" 줄 → (보유, 일부). 괄호 안 메모는 무시."""
    has: list[str] = []
    partial: list[str] = []
    for line in skills_text().splitlines():
        m = re.match(r"\s*-\s*(보유|일부)\s*:\s*(.+)", line)
        if m:
            items = [s.strip() for s in re.sub(r"\([^)]*\)", "", m.group(2)).split(",")]
            (has if m.group(1) == "보유" else partial).extend(s for s in items if s)
    return has, partial
