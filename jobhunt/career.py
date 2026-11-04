"""커리어 관점 추천도 — "지금 나와 맞는가(적합도)" 가 아니라 "이 자리가 내 미래에 얼마나 도움이 되나".

두 부분으로 나눈다:
- 포지션 (LLM, 공고 본문 기준): 전문성 성장 / 업무 범위(PM·리드 경로) / AI 핵심도 / 보상·복지
- 회사 (사이트 태그·회사 페이지 기준, LLM 없이 규칙): 규모(사원수) / 연봉 백분위 / 매출 규모 / 안정성·성장(흑자·매출 성장 포함)

최종 추천도는 대시보드에서 가중치를 조절해 계산한다 (무엇을 중시할지는 사람마다 달라서).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from . import batch_status, llm
from .db import get_conn

BATCH_SIZE = 8
_BODY_LIMIT = 1200

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "posting_id": {"type": "integer"},
                    "depth": {"type": "integer", "minimum": 1, "maximum": 5},
                    "breadth": {"type": "integer", "minimum": 1, "maximum": 5},
                    "ai_core": {"type": "integer", "minimum": 1, "maximum": 5},
                    "benefits": {"type": "integer", "minimum": 1, "maximum": 5},
                    "growth_note": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["posting_id", "depth", "breadth", "ai_core", "benefits", "growth_note", "reason"],
            },
        }
    },
    "required": ["items"],
}

SYSTEM = """너는 한국 IT 업계의 시니어 커리어 코치다.
채용공고를 보고 "AI 엔지니어/AI 풀스택 방향으로 커리어를 쌓으려는 사람에게 이 자리가 장기적으로 얼마나 도움이 되는가"를 평가한다.
지원자가 지금 합격 가능한지는 보지 않는다. 자리 자체의 가치만 본다.

항목별 1~5점 (3 = 보통, 근거가 부족하면 3):
- depth (전문성 성장): 기술적으로 깊이 파고드는 일인가.
  5 = 자체 AI 시스템을 설계·고도화(평가 체계, 대규모 서빙, 에이전트 아키텍처 등)하고 기술 난이도가 높음
  3 = 기존 도구·API 를 조합해 기능을 만드는 수준
  1 = 단순 운영·유지보수, 반복 작업, 라벨링·QA 성격
- breadth (업무 범위 → PM·리드 경로): 문제 정의부터 출시·고객까지 넓게 관여하는가.
  5 = 기획·고객 접점·아키텍처 결정·출시까지 오너십, 다른 직군과 협업, 리드/주도 역할 언급
  3 = 정해진 범위의 개발
  1 = 좁은 단일 업무, 지시받은 작업 위주
- ai_core (AI 핵심도): AI 가 회사·제품의 핵심 사업인가.
  5 = AI 가 주력 제품 그 자체 / 3 = 기존 제품에 AI 기능 추가 / 1 = 사내 업무 보조 정도, AI 가 곁가지
- benefits (보상·복지, 본문 기준): 연봉 수준 명시, 스톡옵션, 인센티브, 교육비·장비·근무제도 등.
  5 = 연봉 상위 명시나 스톡옵션 + 복지 풍부 / 3 = 일반적 복지 / 1 = 언급 거의 없음

growth_note: 이 자리에서 2~3년 일하면 쌓이는 역량을 한국어 한 줄 (예: "B2B 고객 현장에서 RAG 에이전트를 설계·운영한 경험")
reason: 점수 근거를 한국어 1~2문장
"""


@dataclass
class CareerReport:
    processed: int = 0
    calls: int = 0
    errors: list[str] = field(default_factory=list)


def _clip(text: str | None, n: int = _BODY_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …(생략)"


def _render(row) -> str:
    head = [
        f"### posting_id={row['id']}",
        f"제목: {row['title']}",
        f"회사: {row['company']} ({row['company_industry'] or '-'}) / 회사 태그: {row['tags'] or '[]'}",
    ]
    if row["main_tasks"] or row["requirements"]:   # 원티드 — 섹션별
        return "\n".join(head + [
            f"[회사소개]\n{_clip(row['intro'], 500)}",
            f"[주요업무]\n{_clip(row['main_tasks'])}",
            f"[자격요건]\n{_clip(row['requirements'], 600)}",
            f"[우대사항]\n{_clip(row['preferred'], 400)}",
            f"[혜택]\n{_clip(row['benefits'], 600)}",
        ])
    return "\n".join(head + [f"[공고 본문]\n{_clip(row['body'], 3500)}"])   # 사람인·잡코리아 — 통짜


def evaluate(*, max_postings: int = 80, model: str = llm.DEFAULT_MODEL) -> CareerReport:
    """LLM 분석(insights)이 끝난 공고 중 커리어 평가가 없는 것. 적합도 높은 순으로 먼저."""
    rep = CareerReport()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.*, c.tags FROM postings p
               JOIN insights i ON i.posting_id = p.id
               LEFT JOIN career k ON k.posting_id = p.id
               LEFT JOIN companies c ON c.source = p.source AND c.external_id = p.company_id
               WHERE p.status='open' AND p.dup_of IS NULL AND k.posting_id IS NULL
               ORDER BY i.fit_score DESC, p.id DESC
               LIMIT ?""",
            (max_postings,),
        ).fetchall()

        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            prompt = (f"다음 {len(batch)}개 공고를 평가해 items 배열로 반환해. posting_id 는 그대로 유지.\n\n"
                      + "\n\n".join(_render(r) for r in batch))
            rep.calls += 1
            try:
                data = llm.call_json(prompt, system=SYSTEM, schema=SCHEMA, model=model)
            except llm.LLMError as e:
                rep.errors.append(str(e)[:300])
                continue
            valid_ids = {r["id"] for r in batch}
            for it in data.get("items") or []:
                if it.get("posting_id") not in valid_ids:
                    continue
                conn.execute(
                    """INSERT OR REPLACE INTO career (posting_id, model, depth, breadth, ai_core,
                           benefits, growth_note, reason)
                       VALUES (?,?,?,?,?,?,?,?)""",
                    (it["posting_id"], model, it["depth"], it["breadth"], it["ai_core"],
                     it["benefits"], it["growth_note"], it["reason"]),
                )
                rep.processed += 1
            conn.commit()
            batch_status.progress(min(i + BATCH_SIZE, len(rows)), len(rows))
    return rep


# ---- 회사 태그 → 점수 (0~100, 정보 없으면 None) ----------------------------------------
# 태그 문구는 원티드 기준. 새 문구가 나오면 여기만 고치면 된다.

# 2026-11-03 원티드 회사 401곳의 실제 태그 분포를 보고 정함
_SIZE = [  # (정규식, 점수) — 위에서부터 먼저 맞는 것
    (r"10,?001명|1만명|^대기업$", 100), (r"1,?001~10,?000명", 90), (r"301~1,?000명", 75),
    (r"51~300명", 50), (r"11~50명|50명\s*이하", 30), (r"1~10명|10명\s*이하", 15),
]
_FOUNDED = [(r"설립\s*10년\s*이상", 15), (r"설립\s*4~9년", 5), (r"설립\s*3년\s*이하", -10)]
_INVEST = [(r"누적투자\s*1,?000억", 20), (r"누적투자\s*500억", 18), (r"누적투자\s*100억", 15),
           (r"누적투자\s*50억", 8), (r"투자기업$", 5)]
_STANDING = [(r"국내외 TOP 기업|유니콘브릿지|예비 유니콘", 10), (r"아기 유니콘|거래액 우상향", 6)]
_TURNOVER = [(r"퇴사율\s*5%\s*이하", 15), (r"퇴사율\s*10%\s*이하", 8)]
_GROWTH = [(r"인원\s*급성장", 10), (r"인원\s*성장", 6), (r"대규모\s*채용", 5)]
_PAY = re.compile(r"연봉\s*상위\s*(\d+)\s*(?:~\s*(\d+))?\s*%")
_PAY_AVG = re.compile(r"연봉\s*업계\s*평균\s*이상|업계\s*평균\s*이상\s*연봉")
# 규모·연봉·성장·업종·수상 태그를 뺀 나머지 = 복지 태그로 본다
_NON_WELFARE = re.compile(
    r"명|연봉|설립|투자|퇴사율|성장|채용|IT|컨텐츠|금융|제조|^교육$|헬스케어|커머스|게임|모빌리티|부동산|물류"
    r"|기업|유망|스타트업|원티드|인공지능|유니콘|^기타|과학|유통|핀테크|산업|뷰티|IoT|농림|요원|거래액|보건")


def _first(tags: list[str], table) -> int | None:
    for pat, score in table:
        if any(re.search(pat, t) for t in tags):
            return score
    return None


def _size_from_employees(n: int) -> int:
    return 100 if n >= 5000 else 90 if n >= 1000 else 75 if n >= 300 else 50 if n >= 50 else 30 if n >= 10 else 15


def revenue_score(eok: float | None) -> int | None:
    """매출액(억원) → 0~100. 로그 스케일 느낌으로 구간화."""
    if eok is None or eok <= 0:
        return None if eok is None else 5
    for limit, score in [(10, 15), (50, 30), (100, 45), (500, 60), (1000, 75), (5000, 88)]:
        if eok < limit:
            return score
    return 100


def company_scores(tags: list[str], founded_year: int | None = None, fin: dict | None = None) -> dict:
    """회사 태그 + 재무 → {size, pay, stability, revenue, welfare, *_label}.
    fin = {employees, revenue_eok, op_income_eok, revenue_growth, company_type} (사람인·잡코리아 회사 페이지)."""
    fin = fin or {}
    size = _first(tags, _SIZE)
    if fin.get("employees"):   # 실제 사원수(국민연금)가 태그 구간보다 정확
        size = _size_from_employees(fin["employees"])
    ctype = fin.get("company_type") or ""
    if "대기업" in ctype:
        size = max(size or 0, 90)
    elif "중견" in ctype or "코스피" in ctype or "코스닥" in ctype:
        size = max(size or 0, 70)
    pay = None
    pay_label = None
    for t in tags:   # 최신 체계 태그가 앞에 있으므로 첫 매치 사용
        m = _PAY.search(t)
        if m:
            lo, hi = int(m.group(1)), int(m.group(2) or m.group(1))
            pay = max(0, round(100 - (lo + hi) / 2 * 1.5))   # 상위 3% → 96, 8% → 88, 15% → 77, 25% → 62
            pay_label = t
            break
    if pay is None:
        pay_label = next((t for t in tags if _PAY_AVG.search(t)), None)
        pay = 55 if pay_label else None
    parts = [_first(tags, _FOUNDED), _first(tags, _INVEST), _first(tags, _STANDING),
             _first(tags, _TURNOVER), _first(tags, _GROWTH)]
    if founded_year and parts[0] is None:
        age = 2026 - founded_year
        parts[0] = 15 if age >= 10 else 5 if age >= 4 else -10
    op = fin.get("op_income_eok")
    if op is not None:            # 흑자 여부
        parts.append(10 if op > 0 else -10)
    growth = fin.get("revenue_growth")
    if growth is not None:        # 매출 성장
        parts.append(8 if growth >= 20 else 3 if growth >= 0 else -8 if growth < -10 else -3)
    known = [p for p in parts if p is not None]
    stability = max(0, min(100, 50 + sum(known))) if known else None
    emp = fin.get("employees")
    return {
        "size": size, "pay": pay, "stability": stability, "revenue": revenue_score(fin.get("revenue_eok")),
        "pay_label": pay_label,
        "size_label": f"{emp:,}명" if emp else next((t for t in tags if re.search(r"\d+명", t)), None),
        "revenue_eok": fin.get("revenue_eok"), "op_income_eok": op, "revenue_growth": growth,
        "company_type": ctype or None,
        "welfare": [t for t in tags if not _NON_WELFARE.search(t)],
    }
