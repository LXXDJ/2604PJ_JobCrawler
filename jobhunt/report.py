"""알림 + 시장 분석 리포트 (LLM 없이 insights 집계만)."""
from __future__ import annotations

import json
import os
from collections import Counter

from crawlers.infra import config  # noqa: F401  (.env 로딩)
from crawlers.infra.notify import slack_notify

from .db import get_conn

# 팀 프로젝트 SLACK_WEBHOOK_URL 로 새지 않도록 개인용 웹훅만 사용 (fallback 없음)
WEBHOOK = os.getenv("JOBHUNT_SLACK_WEBHOOK_URL", "")

ROLE_LABEL = {
    "llm_app": "LLM 애플리케이션", "ml_engineer": "ML 엔지니어", "ai_fullstack": "AI 풀스택",
    "backend": "백엔드", "data": "데이터", "research": "연구", "other": "기타",
}


def notify_new(*, min_score: int = 65, limit: int = 15, dry_run: bool = False) -> tuple[int, str]:
    """분석 완료 + 아직 알림 안 보낸 고적합 공고를 Slack 으로. Returns (건수, 메시지)."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT p.id, p.title, p.company, p.url, p.due_date, i.fit_score, i.role_type,
                      i.summary, i.gaps, i.english_required
               FROM postings p JOIN insights i ON i.posting_id = p.id
               WHERE p.status='open' AND p.notified_at IS NULL AND i.fit_score >= ?
               ORDER BY i.fit_score DESC LIMIT ?""",
            (min_score, limit),
        ).fetchall()
        if not rows:
            return 0, ""
        lines = [f":dart: *새 매칭 공고 {len(rows)}건* (적합도 {min_score}+)"]
        for r in rows:
            gaps = ", ".join(json.loads(r["gaps"] or "[]")[:3])
            due = r["due_date"] or "상시"
            eng = " :earth_asia:영어필수" if r["english_required"] else ""
            lines.append(
                f"• *[{r['fit_score']}]* <{r['url']}|{r['title']}> — {r['company']} "
                f"({ROLE_LABEL.get(r['role_type'], r['role_type'])}, ~{due}){eng}\n"
                f"   {r['summary']}\n   _갭: {gaps or '-'}_"
            )
        msg = "\n".join(lines)
        if dry_run:
            return len(rows), msg
        if not WEBHOOK:
            raise RuntimeError(".env 에 JOBHUNT_SLACK_WEBHOOK_URL 이 없음")
        if slack_notify(msg, webhook_url=WEBHOOK):
            conn.executemany(
                "UPDATE postings SET notified_at=datetime('now','localtime') WHERE id=?",
                [(r["id"],) for r in rows],
            )
            conn.commit()
        return len(rows), msg


def market_report(*, top: int = 20, fit_min: int = 60) -> str:
    """요구 기술 빈도 / 내 갭 빈도 / 직무 분포. fit_min 이상 = '내가 노릴 만한 공고' 기준 집계."""
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT i.* FROM insights i JOIN postings p ON p.id = i.posting_id
               WHERE p.status='open'"""
        ).fetchall()
    if not rows:
        return "분석된 공고가 없음 — extract 먼저 실행"

    target = [r for r in rows if (r["fit_score"] or 0) >= fit_min]
    req, pref, gaps = Counter(), Counter(), Counter()
    display: dict[str, str] = {}   # 소문자 키 → 처음 본 표기 ("Tensorflow" / "TensorFlow" 합치기)

    def labels(raw: str | None) -> set[str]:
        out = set()
        for s in json.loads(raw or "[]"):
            display.setdefault(s.lower(), s)
            out.add(display[s.lower()])
        return out

    for r in target:
        req.update(labels(r["required_skills"]))
        pref.update(labels(r["preferred_skills"]))
        gaps.update(labels(r["gaps"]))
    roles = Counter(r["role_type"] for r in rows)
    eng = sum(r["english_required"] or 0 for r in target)

    def table(c: Counter, n: int) -> str:
        total = len(target) or 1
        return "\n".join(f"  {k:<28s} {v:>4}  ({v / total:>4.0%})" for k, v in c.most_common(n))

    out = [
        f"=== 시장 리포트: 분석 {len(rows)}건 중 적합도 {fit_min}+ {len(target)}건 기준 ===",
        "\n[직무 분포 — 분석 전체]",
        "\n".join(f"  {ROLE_LABEL.get(k, k):<14s} {v:>4}" for k, v in roles.most_common()),
        f"\n[필수 기술 TOP {top}]", table(req, top),
        f"\n[우대 기술 TOP {top}]", table(pref, top),
        f"\n[내 갭 TOP {top}] — 이걸 채우면 지원 가능 공고가 늘어남", table(gaps, top),
        f"\n영어 필수 공고: {eng}/{len(target)}",
    ]
    return "\n".join(out)
