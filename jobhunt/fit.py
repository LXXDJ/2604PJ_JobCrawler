"""최종 적합도 = LLM 업무 범위 점수 + 코드 기술 매칭 점수.

LLM(extract) 은 프로필의 경력·목표만 보고 scope_score(업무 범위 적합도)와 기술 외 갭(scope_gaps)을 낸다.
기술 매칭:
- 공고에서 추출된 기술 표기마다 "내가 가졌나(보유/일부/없음)" 를 skill_labels 분류표에 한 번만 저장
  (동의어·포함 관계 — "Vector DB" ← pgvector — 는 LLM 이 표기 단위로 판단, 공고 단위 호출 아님)
- 새 표기만 분류하고, 프로필 "## 보유 기술" 섹션이 바뀌면 전체 재분류 (수 분)
- 점수·갭 계산(rescore)은 분류표 조회 + 산술이라 LLM 없이 즉시
"""
from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field

from . import batch_status, llm, profile

SCOPE_WEIGHT = 0.75     # 최종 점수에서 업무 범위(LLM) 비중 — 1순위가 업무 범위 판단이라 크게
REQ_WEIGHT = 0.75       # 기술 점수 안에서 필수 : 우대
PARTIAL_CREDIT = 0.5    # "일부" 기술 인정 비율
# 기술 표기가 이보다 적으면 기술 점수 안 씀 — 기획직처럼 표기 1~2개가 다 보유면 100점이 돼 점수를 끌어올림 (2026-11-04)
MIN_TECH_LABELS = 3

# LLM 이 내는 기술 외 갭 — 이 라벨 중에서만 (여러 공고 집계용)
NON_TECH_GAPS = ["경력 연차", "석사 이상 학위", "논문 실적", "도메인 경험", "영어", "리더십 경험"]

_SEP_RE = re.compile(r"[\s\-_.]+")


def label_key(s: str) -> str:
    """표기 차이 무시용 키 — 대소문자·공백·하이픈·언더스코어·점 제거.
    "Hugging Face"/"HuggingFace", "Tensorflow"/"TensorFlow", "Node.js"/"NodeJS" 를 같은 키로."""
    return _SEP_RE.sub("", s.strip().lower())


# na = 기술 아님 ("Product Design", "English" 등) → 기술 점수 분모·갭 모두에서 뺀다
CREDIT = {"has": 1.0, "partial": PARTIAL_CREDIT, "none": 0.0, "na": None}
CLASSIFY_BATCH = 120    # 한 호출에 분류할 표기 수


def credits(conn) -> dict[str, float | None]:
    """기술 키 → 인정 비율 (None = 기술 아님). 아직 분류 안 된 표기는 없음(0) 으로 본다 (다음 회차에 분류)."""
    return {r["key"]: CREDIT[r["verdict"]]
            for r in conn.execute("SELECT key, verdict FROM skill_labels WHERE verdict IS NOT NULL")}


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["has", "partial", "none", "na"]},
                },
                "required": ["label", "verdict"],
            },
        }
    },
    "required": ["items"],
}

CLASSIFY_SYSTEM = """너는 개발자 이력서와 채용공고 요구 기술을 대조하는 기술 면접관이다.
채용공고에서 추출한 기술 표기 목록을 받아, 아래 지원자 보유 기술 기준으로 각 표기를 분류한다.

- has: 지원자가 그 요구를 충족한다. 같은 기술, 동의어·표기 차이, 호환 기술(Valkey→Redis, OpenSearch→Elasticsearch),
  구체 기술로 상위 개념을 실무에서 해 본 경우(pgvector·OpenSearch 벡터 검색→Vector DB, Azure OpenAI→LLM API·OpenAI API,
  LangGraph 에이전트→AI Agent)를 포함한다
- partial: 관련 경험은 있지만 요구를 온전히 충족한다고 보기 어렵다. 보유 기술의 "일부" 목록에 있는 것,
  상위 개념의 일부만 해 본 경우(Azure OpenAI 만 써 봤는데 "Azure" 전반 요구)
- none: 보유 기술로 충족하지 못하는 기술
- na: 기술이 아니다. 이력서 기술 스택 칸에 적을 특정 언어·프레임워크·라이브러리·도구·플랫폼·기법이 아닌 표기 —
  직무명("Backend Developer"), 일반 업무·역량("Product Design", "Architecture Design", "Monitoring & Logging",
  "Evaluation Framework", "Problem Solving"), 소프트스킬, 도메인("Manufacturing Domain"), 외국어("English"), 학위.
  기술 분야명(RAG, MLOps, Computer Vision, LLM Fine-tuning, Vector DB)은 기술이다 — na 가 아니다

기술이면 보수적으로 판단한다 — 근거가 보유 기술 목록에 없으면 none. 입력 표기를 label 에 그대로 돌려준다.

[지원자 보유 기술]
"""


def _sync_labels(conn) -> None:
    """insights 에 나온 기술 표기를 분류표에 등록 (대표 표기 = 가장 많이 쓰인 표기)."""
    forms: dict[str, Counter] = {}
    for r in conn.execute("SELECT required_skills, preferred_skills FROM insights"):
        for raw in (r["required_skills"], r["preferred_skills"]):
            for s in json.loads(raw or "[]"):
                if s and s.strip():
                    forms.setdefault(label_key(s), Counter())[s.strip()] += 1
    known = {r[0] for r in conn.execute("SELECT key FROM skill_labels")}
    conn.executemany("INSERT INTO skill_labels (key, label) VALUES (?, ?)",
                     [(k, c.most_common(1)[0][0]) for k, c in forms.items() if k not in known])
    conn.commit()


@dataclass
class ClassifyReport:
    labels: int = 0         # 분류표 전체 표기 수
    pending: int = 0        # 이번에 분류 대상이던 표기 수
    processed: int = 0      # 분류 완료
    direct: int = 0         # 보유 기술 목록과 표기가 같아 LLM 없이 분류
    calls: int = 0
    rescored: int = 0       # fit_score·gaps 가 바뀐 공고 수
    errors: list[str] = field(default_factory=list)


def classify(conn, *, model: str = "sonnet", force: bool = False) -> ClassifyReport:
    """새 표기 + 보유 기술 섹션 변경 후 미갱신 표기를 분류. force=True 면 고정값 외 전체 재분류."""
    rep = ClassifyReport()
    _sync_labels(conn)
    h = profile.skills_hash()
    has, partial = profile.my_skills()
    direct = {label_key(s): "partial" for s in partial}
    direct.update({label_key(s): "has" for s in has})

    where = "pinned = 0" + ("" if force else " AND (verdict IS NULL OR coalesce(skills_hash, '') != ?)")
    rows = conn.execute(f"SELECT key, label FROM skill_labels WHERE {where} ORDER BY key",
                        () if force else (h,)).fetchall()
    rep.labels = conn.execute("SELECT COUNT(*) FROM skill_labels").fetchone()[0]
    rep.pending = len(rows)

    def save(key: str, verdict: str) -> None:
        conn.execute("""UPDATE skill_labels SET verdict=?, skills_hash=?, classified_at=datetime('now','localtime')
                        WHERE key=?""", (verdict, h, key))

    todo = []
    for r in rows:
        if r["key"] in direct:      # 목록에 그대로 있는 표기는 LLM 불필요
            save(r["key"], direct[r["key"]])
            rep.direct += 1
            rep.processed += 1
        else:
            todo.append(r)
    conn.commit()

    system = CLASSIFY_SYSTEM + profile.skills_text()
    for i in range(0, len(todo), CLASSIFY_BATCH):
        batch = todo[i:i + CLASSIFY_BATCH]
        keys = {r["key"] for r in batch}
        prompt = "다음 기술 표기를 분류해 items 배열로 반환해.\n\n" + "\n".join(r["label"] for r in batch)
        rep.calls += 1
        try:
            data = llm.call_json(prompt, system=system, schema=CLASSIFY_SCHEMA, model=model)
        except llm.LLMError as e:
            rep.errors.append(str(e)[:300])
            continue
        for it in data.get("items") or []:
            k = label_key(it.get("label") or "")
            if k in keys and it.get("verdict") in CREDIT:
                save(k, it["verdict"])
                rep.processed += 1
        conn.commit()
        batch_status.progress(min(i + CLASSIFY_BATCH, len(todo)), len(todo))
    return rep


def pin(conn, label: str, verdict: str) -> None:
    """분류 직접 고정 (재분류 대상에서 제외). verdict='auto' 면 고정 해제 → 다음 회차에 다시 분류."""
    k = label_key(label)
    if verdict == "auto":
        conn.execute("UPDATE skill_labels SET pinned=0, skills_hash=NULL WHERE key=?", (k,))
    else:
        conn.execute("""INSERT INTO skill_labels (key, label, verdict, pinned, classified_at)
                        VALUES (?, ?, ?, 1, datetime('now','localtime'))
                        ON CONFLICT(key) DO UPDATE SET verdict=excluded.verdict, pinned=1,
                                                       classified_at=excluded.classified_at""",
                     (k, label.strip(), verdict))
    conn.commit()


def _clean(skills: list[str]) -> list[str]:
    return [s.strip() for s in skills if s and s.strip()]


def _tech(skills: list[str], cr: dict[str, float | None]) -> list[str]:
    """기술 아님(na) 표기 제외."""
    return [s for s in _clean(skills) if cr.get(label_key(s), 0.0) is not None]


def skill_score(req: list[str], pref: list[str], cr: dict[str, float | None]) -> int | None:
    """0~100. 공고의 기술 표기가 MIN_TECH_LABELS 개 미만이면 None (업무 범위 점수만 씀)."""
    req, pref = _tech(req, cr), _tech(pref, cr)

    def cov(lst: list[str]) -> float:
        return sum(cr.get(label_key(s), 0.0) for s in lst) / len(lst)

    if len({label_key(s) for s in req + pref}) < MIN_TECH_LABELS:
        return None
    if not pref:
        r = cov(req)
    elif not req:
        r = cov(pref)
    else:
        r = REQ_WEIGHT * cov(req) + (1 - REQ_WEIGHT) * cov(pref)
    return round(r * 100)


def tech_gaps(req: list[str], pref: list[str], cr: dict[str, float | None]) -> list[str]:
    """보유가 아닌(없음 또는 일부) 기술 — 필수 먼저, 표기 중복 제거."""
    out, seen = [], set()
    for s in _tech(req, cr) + _tech(pref, cr):
        k = label_key(s)
        if k not in seen and cr.get(k, 0.0) < 1.0:
            seen.add(k)
            out.append(s)
    return out


def combine(scope: int, skill: int | None) -> int:
    return scope if skill is None else round(SCOPE_WEIGHT * scope + (1 - SCOPE_WEIGHT) * skill)


def score_row(req: list[str], pref: list[str], scope: int, scope_gaps: list[str],
              cr: dict[str, float | None]) -> tuple[int, int | None, list[str]]:
    """(fit_score, skill_score, gaps)."""
    sk = skill_score(req, pref, cr)
    return combine(scope, sk), sk, tech_gaps(req, pref, cr) + list(scope_gaps)


def rescore(conn) -> int:
    """scope_score 가 있는 모든 insights 의 fit_score·skill_score·gaps 재계산. 바뀐 행 수 반환."""
    cr = credits(conn)
    changed = 0
    rows = conn.execute(
        """SELECT posting_id, required_skills, preferred_skills, scope_score, scope_gaps,
                  fit_score, skill_score, gaps
           FROM insights WHERE scope_score IS NOT NULL""").fetchall()
    for r in rows:
        fit, sk, gaps = score_row(json.loads(r["required_skills"] or "[]"), json.loads(r["preferred_skills"] or "[]"),
                                  r["scope_score"], json.loads(r["scope_gaps"] or "[]"), cr)
        gaps_json = json.dumps(gaps, ensure_ascii=False)
        if (fit, sk, gaps_json) != (r["fit_score"], r["skill_score"], r["gaps"]):
            conn.execute("UPDATE insights SET fit_score=?, skill_score=?, gaps=? WHERE posting_id=?",
                         (fit, sk, gaps_json, r["posting_id"]))
            changed += 1
    conn.commit()
    return changed
