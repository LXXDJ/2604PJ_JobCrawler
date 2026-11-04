"""LLM 구조화 추출 + 적합도 — 상세 수집된 매칭 공고 중 아직 insights 없는 것만.

구독 한도 보호:
- 공고 BATCH_SIZE 건을 한 번의 호출로 묶음
- 회차당 max_postings 건까지만 처리 (나머지는 다음 회차)
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import llm, profile
from .db import get_conn

BATCH_SIZE = 6
_BODY_LIMIT = 1500          # 섹션당 글자수 상한 (회사소개·복지는 분석에 덜 중요해서 더 짧게)

ROLE_TYPES = ["llm_app", "ml_engineer", "ai_fullstack", "backend", "data", "research", "other"]

SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "posting_id": {"type": "integer"},
                    "role_type": {"type": "string", "enum": ROLE_TYPES},
                    "summary": {"type": "string"},
                    "years_min": {"type": ["integer", "null"]},
                    "years_max": {"type": ["integer", "null"]},
                    "required_skills": {"type": "array", "items": {"type": "string"}},
                    "preferred_skills": {"type": "array", "items": {"type": "string"}},
                    "domain": {"type": "string"},
                    "english_required": {"type": "boolean"},
                    "salary_text": {"type": ["string", "null"]},
                    "fit_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "fit_reason": {"type": "string"},
                    "gaps": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["posting_id", "role_type", "summary", "years_min", "years_max",
                             "required_skills", "preferred_skills", "domain", "english_required",
                             "salary_text", "fit_score", "fit_reason", "gaps"],
            },
        }
    },
    "required": ["items"],
}

SYSTEM = """너는 한국 IT 채용공고를 분석하는 커리어 어드바이저다.
여러 공고를 받아 각 공고마다 구조화된 정보를 추출하고, 아래 지원자 프로필과의 적합도를 평가한다.

추출 규칙:
- role_type: llm_app(LLM/RAG/Agent 애플리케이션), ml_engineer(모델 학습·서빙·MLOps), ai_fullstack(AI 기능이 들어간 제품의 FE+BE),
  backend(일반 백엔드), data(데이터 엔지니어/분석), research(연구직), other
- required_skills / preferred_skills: 기술·도구·역량을 짧은 표준 영문 표기로 정규화 (예: "Python", "FastAPI", "LangChain",
  "RAG", "AWS", "Kubernetes", "Docker", "PyTorch", "Spring Boot", "React", "Vector DB", "LLM Fine-tuning", "MLOps").
  소프트스킬("커뮤니케이션 능력"), 학위("석사 이상"), 연차, 도메인 지식("패션 도메인 이해"), 논문·대회 실적은
  required/preferred 어느 쪽 skills 에도 넣지 않는다.
  특정 기술·도구가 아닌 일반 역량("Problem Solving", "Code Review", "Framework Implementation",
  "Service Deployment", "Backend Integration" 등)도 넣지 않는다. 이력서 기술 스택 칸에 적을 수 있는 것만 넣는다.
  학위·도메인 요건은 gaps 와 fit_reason 에서만 다룬다.
- years_min/years_max: 본문 기준. 언급 없으면 null.
- domain: 산업/서비스 도메인을 짧게 (예: "핀테크", "이커머스", "헬스케어", "B2B SaaS", "제조", "교육")
- english_required: 영어 회화/비즈니스 영어가 필수 요건이면 true (우대는 false)
- salary_text: 본문에 연봉 정보가 있으면 원문 그대로, 없으면 null
- summary: 무슨 일을 하는 포지션인지 한국어 1~2문장

적합도 규칙 (fit_score 0~100):
- 평가 시점은 오늘이 아니라 프로필에 적힌 "지원 예정 시점"이다. 그 시점의 경력·학위·자격 기준으로 판단한다
- 서류 통과 가능성 + 지원자의 목표·선호와의 일치도를 함께 본다
- 80+: 목표 직무와 정확히 맞고 필수요건 대부분 충족 / 60~79: 맞지만 일부 요건 부족 / 40~59: 인접 직무거나 갭이 큼 / 40 미만: 방향이 다름
- fit_reason: 한국어 1~2문장
- gaps: 지원자가 부족한 필수/우대 요건. 여러 공고에 걸쳐 집계하므로 반드시 짧은 표준 라벨로 쓴다.
  · 기술 갭은 required/preferred_skills 에 쓴 표기 그대로 (예: "Kubernetes", "AWS", "LLM Fine-tuning", "PyTorch")
    — "Docker/Kubernetes 경험" 처럼 묶거나 꾸미지 말고 "Docker", "Kubernetes" 로 나눈다
  · 기술 외 갭은 다음 고정 라벨 중에서만: "경력 연차", "석사 이상 학위", "논문 실적", "도메인 경험", "영어", "리더십 경험"

[지원자 프로필]
"""


@dataclass
class ExtractReport:
    processed: int = 0
    calls: int = 0
    errors: list[str] = field(default_factory=list)


def _clip(text: str | None, n: int = _BODY_LIMIT) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[:n] + " …(생략)"


# LLM 에 보낼 본문이 있는 공고만 — 이미지 통짜 공고(텍스트 거의 없음)는 제목만으로 분석하면 틀리므로 제외
ANALYZABLE_SQL = ("(p.main_tasks IS NOT NULL OR p.requirements IS NOT NULL"
                  " OR length(coalesce(p.body, '')) >= 150)")


def _render(row) -> str:
    head = [
        f"### posting_id={row['id']}",
        f"제목: {row['title']}",
        f"회사: {row['company']} ({row['company_industry'] or '-'}) / 위치: {row['location'] or '-'}",
        f"표기 경력: {row['years_from']}~{row['years_to']}년 / 기술태그: {row['skill_tags'] or '[]'}",
    ]
    if row["main_tasks"] or row["requirements"]:   # 원티드 — 섹션별
        return "\n".join(head + [
            f"[회사소개]\n{_clip(row['intro'], 600)}",
            f"[주요업무]\n{_clip(row['main_tasks'])}",
            f"[자격요건]\n{_clip(row['requirements'])}",
            f"[우대사항]\n{_clip(row['preferred'])}",
            f"[혜택]\n{_clip(row['benefits'], 400)}",
        ])
    return "\n".join(head + [f"[공고 본문]\n{_clip(row['body'], 4000)}"])   # 사람인·잡코리아 — 통짜


def extract(*, max_postings: int = 60, model: str = llm.DEFAULT_MODEL) -> ExtractReport:
    rep = ExtractReport()
    system = SYSTEM + profile.profile_text()
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.* FROM postings p LEFT JOIN insights i ON i.posting_id = p.id
               WHERE p.matched=1 AND p.status='open' AND p.detail_fetched_at IS NOT NULL
                 AND p.dup_of IS NULL AND """ + ANALYZABLE_SQL + """
                 AND i.posting_id IS NULL
               ORDER BY p.first_seen_at DESC, p.id DESC
               LIMIT ?""",
            (max_postings,),
        ).fetchall()

        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            prompt = (f"다음 {len(batch)}개 공고를 분석해 items 배열로 반환해. posting_id 는 그대로 유지.\n\n"
                      + "\n\n".join(_render(r) for r in batch))
            rep.calls += 1
            try:
                data = llm.call_json(prompt, system=system, schema=SCHEMA, model=model)
            except llm.LLMError as e:
                rep.errors.append(str(e)[:300])
                continue
            valid_ids = {r["id"] for r in batch}
            for it in data.get("items") or []:
                if it.get("posting_id") not in valid_ids:
                    continue
                conn.execute(
                    """INSERT OR REPLACE INTO insights (posting_id, model, role_type, summary,
                           years_min, years_max, required_skills, preferred_skills, domain,
                           english_required, salary_text, fit_score, fit_reason, gaps)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (it["posting_id"], model, it["role_type"], it["summary"],
                     it["years_min"], it["years_max"],
                     json.dumps(it["required_skills"], ensure_ascii=False),
                     json.dumps(it["preferred_skills"], ensure_ascii=False),
                     it["domain"], int(bool(it["english_required"])), it["salary_text"],
                     it["fit_score"], it["fit_reason"],
                     json.dumps(it["gaps"], ensure_ascii=False)),
                )
                rep.processed += 1
            conn.commit()
    return rep
