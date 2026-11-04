"""LLM 구조화 추출 + 업무 범위 적합도 — 상세 수집된 매칭 공고 중 아직 insights 없는 것만.

LLM 은 기술 목록을 뺀 프로필(경력·목표)로 scope_score 만 매긴다. 최종 fit_score·gaps 는
fit.py 가 기술 분류표로 계산 (기술 스택만 바뀌면 재분석 불필요).

구독 한도 보호:
- 공고 BATCH_SIZE 건을 한 번의 호출로 묶음
- 회차당 max_postings 건까지만 처리 (나머지는 다음 회차)

정확도 (2026-11-04, 같은 30건 2회씩 비교):
- 모델 sonnet — haiku 는 "방향이 다르다" 고 쓰고 65점을 주는 등 점수·근거 불일치, 기획직을 "목표와 일치" 로 오판.
  처리 시간은 같음 (30건 11분)
- 같은 공고도 회차마다 ±5~7점 흔들림 → 경계 구간(RECHECK_RANGE)만 한 번 더 판단해 평균
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import batch_status, fit, llm, profile
from .db import get_conn

BATCH_SIZE = 6
MODEL = "sonnet"
RECHECK_RANGE = (60, 85)    # 업무 범위가 이 구간이면 재판단 — 알림 기준(report.HIGH_FIT) 근처라 흔들림이 결과를 바꿈
_BODY_LIMIT = 1500         # 섹션당 글자수 상한 (회사소개·복지는 분석에 덜 중요해서 더 짧게)

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
                    "scope_score": {"type": "integer", "minimum": 0, "maximum": 100},
                    "fit_reason": {"type": "string"},
                    "scope_gaps": {"type": "array", "items": {"type": "string", "enum": fit.NON_TECH_GAPS}},
                },
                "required": ["posting_id", "role_type", "summary", "years_min", "years_max",
                             "required_skills", "preferred_skills", "domain", "english_required",
                             "salary_text", "scope_score", "fit_reason", "scope_gaps"],
            },
        }
    },
    "required": ["items"],
}

SYSTEM = """너는 한국 IT 채용공고를 분석하는 커리어 어드바이저다.
여러 공고를 받아 각 공고마다 구조화된 정보를 추출하고, 아래 지원자 프로필 기준으로 업무 범위 적합도를 평가한다.

추출 규칙:
- role_type: llm_app(LLM/RAG/Agent 애플리케이션), ml_engineer(모델 학습·서빙·MLOps), ai_fullstack(AI 기능이 들어간 제품의 FE+BE),
  backend(일반 백엔드), data(데이터 엔지니어/분석), research(연구직), other
- required_skills / preferred_skills: 기술·도구·역량을 짧은 표준 영문 표기로 정규화 (예: "Python", "FastAPI", "LangChain",
  "RAG", "AWS", "Kubernetes", "Docker", "PyTorch", "Spring Boot", "React", "Vector DB", "LLM Fine-tuning", "MLOps").
  소프트스킬("커뮤니케이션 능력"), 학위("석사 이상"), 연차, 도메인 지식("패션 도메인 이해"), 논문·대회 실적은
  required/preferred 어느 쪽 skills 에도 넣지 않는다.
  특정 기술·도구가 아닌 일반 역량("Problem Solving", "Code Review", "Framework Implementation",
  "Service Deployment", "Backend Integration" 등)도 넣지 않는다. 이력서 기술 스택 칸에 적을 수 있는 것만 넣는다.
  학위·도메인 요건은 scope_gaps 와 fit_reason 에서만 다룬다.
- years_min/years_max: 본문 기준. 언급 없으면 null.
- domain: 산업/서비스 도메인을 짧게 (예: "핀테크", "이커머스", "헬스케어", "B2B SaaS", "제조", "교육")
- english_required: 영어 회화/비즈니스 영어가 필수 요건이면 true (우대는 false)
- salary_text: 본문에 연봉 정보가 있으면 원문 그대로, 없으면 null
- summary: 무슨 일을 하는 포지션인지 한국어 1~2문장

업무 범위 적합도 규칙 (scope_score 0~100):
- 기술 스택 일치(특정 도구·프레임워크를 써 봤는지)는 별도로 계산하므로 점수에 넣지 않는다.
  이 포지션이 실제로 하는 일(주요업무·책임 범위)이 지원자가 해 온 일, 목표 직무와 얼마나 맞는지로 판단한다
  (예: LLM 애플리케이션·RAG·에이전트 서비스 개발 / 모델 학습·연구 / 일반 백엔드 / 데이터 엔지니어링 / 하드웨어 연계)
- 평가 시점은 오늘이 아니라 프로필에 적힌 "지원 예정 시점"이다. 그 시점의 경력·학위·자격 기준으로 판단한다
- 하는 일의 성격과 함께 요구 연차, 학위·논문 요건, 도메인, 영어 필수 여부, 지원자의 목표·선호·비선호를 본다
- 연차 비교:
  · 지원자 연차는 프로필 "연차" 줄의 값만 쓴다. 날짜로 직접 계산하지 않는다
  · 공고 요구 연차는 그 직무 분야 연차와 비교한다 — AI·ML·LLM·데이터 사이언스 직무는 AI 실무 연차,
    백엔드 직무는 백엔드 연차, 프론트엔드 직무는 프론트엔드 연차, 분야를 특정하지 않은 "개발 경력 N년"은 전체 개발 경력,
    리드·PL·매니저 요건은 경력에 적힌 리드 경험
  · 충족·미충족만 판단한다. "충족 예상", "곧 충족" 같은 표현은 쓰지 않는다
  · 1년 이하 부족은 경미(60~79 구간 안에서 감점), 1년 넘게 부족하면 큰 갭(40~59 구간). 상한 초과는 감점하지 않는다
  · 미충족이면 scope_gaps 에 "경력 연차"를 넣고 fit_reason 에 "AI 실무 2년 / 요구 3년+" 처럼 수치를 쓴다
- 80+: 하는 일이 목표 직무와 정확히 맞고 지원자가 해 온 일과 크게 겹침
  60~79: 방향은 맞지만 하는 일 일부가 경험 밖이거나 연차·도메인 등 조건이 일부 안 맞음
  40~59: 인접 직무거나 조건 갭이 큼 / 40 미만: 방향이 다름
- fit_reason: 한국어 1~2문장. 하는 일이 지원자 경력과 어디서 맞고 어디서 어긋나는지 (기술 스택 일치 여부는 쓰지 않는다)
- scope_gaps: 기술 외 부족 요건만, 고정 라벨 중에서: "경력 연차", "석사 이상 학위", "논문 실적", "도메인 경험", "영어", "리더십 경험"

[지원자 프로필]
"""


@dataclass
class ExtractReport:
    processed: int = 0
    calls: int = 0
    rechecked: int = 0      # 경계 구간이라 두 번 판단해 평균 낸 공고 수
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


def _prompt(rows) -> str:
    return (f"다음 {len(rows)}개 공고를 분석해 items 배열로 반환해. posting_id 는 그대로 유지.\n\n"
            + "\n\n".join(_render(r) for r in rows))


def _recheck(rows, items: list[dict], *, system: str, model: str, rep: ExtractReport) -> None:
    """경계 구간 공고만 다시 판단해 scope_score 를 두 번의 평균으로 (items 를 제자리 수정).
    근거·기술 외 갭은 첫 판단 것을 유지. 재판단이 실패하면 첫 점수 그대로."""
    targets = {it["posting_id"]: it for it in items
               if RECHECK_RANGE[0] <= it["scope_score"] <= RECHECK_RANGE[1]}
    if not targets:
        return
    rep.calls += 1
    try:
        data = llm.call_json(_prompt([r for r in rows if r["id"] in targets]),
                             system=system, schema=SCHEMA, model=model)
    except llm.LLMError as e:
        rep.errors.append(f"재판단: {str(e)[:280]}")
        return
    for b in data.get("items") or []:
        it = targets.get(b.get("posting_id"))
        if it and isinstance(b.get("scope_score"), int):
            it["scope_score"] = round((it["scope_score"] + b["scope_score"]) / 2)
            rep.rechecked += 1


def extract(*, max_postings: int = 60, model: str = MODEL,
            refit_min: int | None = None) -> ExtractReport:
    """refit_min 을 주면 신규 대신 '적합도 refit_min 이상 + 지금 프로필(경력·목표) 버전으로 분석 안 된' 공고를
    다시 분석. 이미 현재 버전으로 분석된 공고는 건너뛰므로 중간에 끊겨도 다시 돌리면 이어서 한다.
    fit_score·gaps 는 여기서 현재 분류표로 바로 계산 (새 기술 표기는 다음 classify 후 rescore 로 반영)."""
    rep = ExtractReport()
    system = SYSTEM + profile.profile_text()
    phash = profile.profile_hash()
    if refit_min is None:
        target_sql, order_sql, params = "i.posting_id IS NULL", "p.first_seen_at DESC, p.id DESC", ()
    else:
        # 오늘 마감인 공고는 오늘 밤 배치에서 닫히므로 재분석 생략
        target_sql = ("i.fit_score >= ? AND coalesce(i.profile_hash, '') != ?"
                      " AND (p.due_date IS NULL OR p.due_date > date('now','localtime'))")
        order_sql = "i.fit_score DESC, p.id DESC"
        params = (refit_min, phash)
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.* FROM postings p LEFT JOIN insights i ON i.posting_id = p.id
               WHERE p.matched=1 AND p.status='open' AND p.detail_fetched_at IS NOT NULL
                 AND p.dup_of IS NULL AND """ + ANALYZABLE_SQL + """
                 AND """ + target_sql + """
               ORDER BY """ + order_sql + """
               LIMIT ?""",
            (*params, max_postings),
        ).fetchall()
        cr = fit.credits(conn)

        for i in range(0, len(rows), BATCH_SIZE):
            batch = rows[i:i + BATCH_SIZE]
            rep.calls += 1
            try:
                data = llm.call_json(_prompt(batch), system=system, schema=SCHEMA, model=model)
            except llm.LLMError as e:
                rep.errors.append(str(e)[:300])
                continue
            valid_ids = {r["id"] for r in batch}
            items = [it for it in data.get("items") or [] if it.get("posting_id") in valid_ids]
            _recheck(batch, items, system=system, model=model, rep=rep)
            for it in items:
                scope_gaps = [g for g in it["scope_gaps"] if g in fit.NON_TECH_GAPS]
                score, skill, gaps = fit.score_row(it["required_skills"], it["preferred_skills"],
                                                   it["scope_score"], scope_gaps, cr)
                conn.execute(
                    """INSERT OR REPLACE INTO insights (posting_id, model, role_type, summary,
                           years_min, years_max, required_skills, preferred_skills, domain,
                           english_required, salary_text, fit_score, fit_reason, gaps,
                           scope_score, scope_gaps, skill_score, profile_hash)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (it["posting_id"], model, it["role_type"], it["summary"],
                     it["years_min"], it["years_max"],
                     json.dumps(it["required_skills"], ensure_ascii=False),
                     json.dumps(it["preferred_skills"], ensure_ascii=False),
                     it["domain"], int(bool(it["english_required"])), it["salary_text"],
                     score, it["fit_reason"], json.dumps(gaps, ensure_ascii=False),
                     it["scope_score"], json.dumps(scope_gaps, ensure_ascii=False), skill, phash),
                )
                rep.processed += 1
            conn.commit()
            batch_status.progress(min(i + BATCH_SIZE, len(rows)), len(rows))
    return rep
