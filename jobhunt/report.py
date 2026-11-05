"""알림 + 시장 분석 리포트 (LLM 없이 insights 집계만)."""
from __future__ import annotations

import json
import os
from collections import Counter
from datetime import datetime, timedelta

from crawlers.infra import config  # noqa: F401  (.env 로딩)
from crawlers.infra.notify import slack_notify

from . import batch_status
from .batch_status import STEP_LABEL, STEPS
from .db import get_conn
from .extract import ANALYZABLE_SQL
from .fit import label_key as _label_key
from .ocr import OCR_MAX_FAILS

# 팀 프로젝트 SLACK_WEBHOOK_URL 로 새지 않도록 개인용 웹훅만 사용 (fallback 없음)
WEBHOOK = os.getenv("JOBHUNT_SLACK_WEBHOOK_URL", "")

# 고적합 기준 — Slack 알림(배치 완료·notify)과 대시보드 강조가 모두 이 값을 쓴다
# 2026-11-04 sonnet 전환 후 70: 목표와 정확히 맞는 공고가 72~74 에 몰려 75 면 흔들림 몇 점에 빠짐
HIGH_FIT = 70

ROLE_LABEL = {
    "llm_app": "LLM 애플리케이션", "ml_engineer": "ML 엔지니어", "ai_fullstack": "AI 풀스택",
    "backend": "백엔드", "data": "데이터", "research": "연구", "other": "기타",
}

def notify_new(*, min_score: int = HIGH_FIT, limit: int = 15, dry_run: bool = False) -> tuple[int, str]:
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


def send(msg: str) -> None:
    if not WEBHOOK:
        raise RuntimeError(".env 에 JOBHUNT_SLACK_WEBHOOK_URL 이 없음")
    if not slack_notify(msg, webhook_url=WEBHOOK):
        raise RuntimeError("Slack 전송 실패")


# ---- 매일 배치 완료 알림 / 오전 8시 점검 알림 ------------------------------------------------

SOURCE_LABEL = {"wanted": "원티드", "saramin": "사람인", "jobkorea": "잡코리아"}
STALL_MIN = 15          # 이 시간 넘게 진행 기록이 없으면 멈춘 것으로 본다
_OPEN_REP = "FROM postings p WHERE p.matched=1 AND p.status='open' AND p.dup_of IS NULL"


def _ts(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


def _day(dt: datetime) -> str:
    return f"{dt:%m/%d} ({'월화수목금토일'[dt.weekday()]})"


def _dur(a: datetime, b: datetime) -> str:
    m = int((b - a).total_seconds() // 60)
    return f"{m // 60}시간 {m % 60}분" if m >= 60 else f"{m}분"


def _bar(ratio: float) -> str:
    n = round(ratio * 10)
    return "▓" * n + "░" * (10 - n)


def _esc(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _years(lo: int | None, hi: int | None) -> str:
    if lo is None:
        return ""
    if hi == 100:
        return "경력무관" if lo == 0 else f"경력 {lo}년↑"
    if lo == 0:
        return "신입" if hi == 0 else f"신입~{hi}년"
    return f"경력 {lo}~{hi}년"


def _stuck_reason(r) -> str:
    if (r["ocr_fails"] or 0) >= OCR_MAX_FAILS:
        return "이미지 읽기 실패"
    if not json.loads(r["body_images"] or "[]"):
        return "본문·이미지 없음"
    return "이미지에서 읽은 글자가 너무 적음"


def _steps_line(st: dict) -> str:
    done = set(st.get("completed_steps") or [])
    failed = {f["step"] for f in st.get("failures") or []}
    marks = []
    for name, label in STEPS:
        mark = "✅" if name in done else "❌" if name in failed else "▶" if name == st.get("step") else ""
        marks.append(f"{label} {mark}".strip())
    return " · ".join(marks)


def daily_done_message(st: dict, *, min_score: int = HIGH_FIT, top: int = 5) -> tuple[str, list[int]]:
    """배치 정상 완료 알림. Returns (메시지, 알림 처리할 고적합 공고 id)."""
    start, end = _ts(st["started_at"]), datetime.now()
    col = st.get("collect") or {}
    with get_conn() as conn:
        q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
        open_all = q("SELECT COUNT(*) FROM postings WHERE status='open'")
        matched = q(f"SELECT COUNT(*) {_OPEN_REP}")
        by_src = dict(conn.execute(f"SELECT p.source, COUNT(*) {_OPEN_REP} GROUP BY p.source").fetchall())
        analyzed = q(f"SELECT COUNT(*) {_OPEN_REP} AND p.id IN (SELECT posting_id FROM insights)")
        careered = q(f"SELECT COUNT(*) {_OPEN_REP} AND p.id IN (SELECT posting_id FROM career)")
        # 내일 배치로 넘겨도 처리 안 되는 공고 — 상세는 받았는데 본문이 짧고 OCR 도 끝(포기)난 것
        stuck = conn.execute(
            f"""SELECT p.company, p.title, p.url, p.body_images, p.ocr_fails {_OPEN_REP}
                  AND p.id NOT IN (SELECT posting_id FROM insights) AND p.detail_fetched_at IS NOT NULL
                  AND p.ocr_at IS NOT NULL AND NOT {ANALYZABLE_SQL}
                ORDER BY p.first_seen_at DESC""").fetchall()
        hot = conn.execute(
            f"""SELECT p.id, p.title, p.company, p.url, p.years_from, p.years_to, i.fit_score
                FROM postings p JOIN insights i ON i.posting_id = p.id
                WHERE p.matched=1 AND p.status='open' AND p.dup_of IS NULL AND p.notified_at IS NULL
                  AND i.fit_score >= ?
                ORDER BY i.fit_score DESC, p.id DESC""",
            (min_score,),
        ).fetchall()

    total = matched or 1
    src_txt = " · ".join(f"{SOURCE_LABEL.get(s, s)} {by_src.get(s, 0):,}" for s in SOURCE_LABEL)
    left = matched - analyzed
    lines = [
        f"✅ 이직 공고 배치 완료 — {_day(start)} {start:%H:%M} → {end:%H:%M} ({_dur(start, end)})",
        "",
        "📥 수집",
        f"  • 전체 공고 {open_all:,}건" + (f" (오늘 신규 {col['new']:,}건)" if "new" in col else ""),
        f"  • 내 조건에 맞는 공고 {matched:,}건 (오늘 신규 {col.get('new_matched', 0):,}건)  ← {src_txt}",
        f"  • 마감 처리 {col.get('closed', 0):,}건",
        "",
        "🤖 LLM 분석 진행률",
        f"  • 공고 분석   {analyzed:,} / {matched:,}건  ({analyzed / total:.1%})  {_bar(analyzed / total)}",
        f"  • 커리어 평가 {careered:,} / {matched:,}건  ({careered / total:.1%})  {_bar(careered / total)}",
    ]
    if not left:
        lines.append("  • 모든 공고 분석 완료")
    else:
        lines.append(f"  • 남은 분석 {left:,}건")
        if left > len(stuck):
            lines.append(f"     – 내일 배치에서 처리 {left - len(stuck):,}건")
        if stuck:
            lines.append(f"     – 자동 분석 불가 {len(stuck)}건 → 직접 확인")
            for r in stuck[:top]:
                lines.append(f"         · {_esc(r['company'])} — <{r['url']}|{_esc(r['title'])}> ({_stuck_reason(r)})")
            if len(stuck) > top:
                lines.append(f"         · 외 {len(stuck) - top}건")
    if hot:
        lines += ["", f"⭐ 오늘 새로 들어온 고적합 공고 (적합도 {min_score}점 이상) {len(hot)}건"]
        for n, r in enumerate(hot[:top], 1):
            yrs = _years(r["years_from"], r["years_to"])
            lines.append(f"  {n}. [{r['fit_score']}] {_esc(r['company'])} — <{r['url']}|{_esc(r['title'])}>"
                         + (f" ({yrs})" if yrs else ""))
        if len(hot) > top:
            lines.append(f"  (상위 {top}건만 표시, 나머지는 대시보드에서)")
    lines += ["", "🔗 대시보드: http://127.0.0.1:8787"]
    return "\n".join(lines), [r["id"] for r in hot]


def mark_notified(ids: list[int]) -> None:
    with get_conn() as conn:
        conn.executemany("UPDATE postings SET notified_at=datetime('now','localtime') WHERE id=?",
                         [(i,) for i in ids])
        conn.commit()


def _step_progress(st: dict) -> str:
    done, total = st.get("step_done"), st.get("step_total")
    if total:
        return f"{done:,} / {total:,}건 ({done / total:.0%})"
    return "건수 집계 전"


def daily_check_message(now: datetime | None = None) -> str | None:
    """오전 8시 점검 — 오늘 배치가 정상 완료됐으면 None, 아니면 진행 중 / 오류 / 미실행 메시지."""
    now = now or datetime.now()
    st = batch_status.load()
    start = _ts(st.get("started_at"))
    head = f"{_day(now)} {now:%H:%M} 기준"

    if not start or start.date() != now.date():
        last = _ts(st.get("finished_at") if st.get("state") == "done" else st.get("last_done_at"))
        return "\n".join([
            f"⚠️ 이직 공고 배치 미실행 — {head}",
            "  • 오늘 00:00 배치 기록이 없어요 (PC가 꺼져 있었거나 절전 상태였을 수 있어요)",
            f"  • 마지막 정상 완료: {_day(last)} {last:%H:%M}" if last else "  • 마지막 정상 완료: 기록 없음",
        ])
    if st.get("state") == "done":
        return None

    if st.get("state") == "failed":
        end = _ts(st["finished_at"])
        lines = [f"❌ 이직 공고 배치 오류 — {head}",
                 f"  • 시작 {start:%H:%M} → {end:%H:%M}에 끝났지만 오류가 있어 완료 알림을 보내지 않았어요"]
        for f in st.get("failures") or []:
            prog = f" ({f['done']:,} / {f['total']:,}건까지 처리)" if f.get("total") else ""
            lines.append(f"  • {STEP_LABEL.get(f['step'], f['step'])} 단계 실패{prog}: {f['error']}")
        lines.append("  → 로그: ~/jobhunt-data/logs/jobhunt.log")
        return "\n".join(lines)

    # running
    step = st.get("step")
    idx = next((n for n, (name, _) in enumerate(STEPS, 1) if name == step), 0)
    step_txt = f"{STEP_LABEL.get(step, step)} ({idx}/{len(STEPS)})"
    last_prog = _ts(st.get("progress_at")) or start
    alive = batch_status.pid_alive(st.get("pid"))
    if not alive or now - last_prog > timedelta(minutes=STALL_MIN):
        cause = ("프로세스가 중간에 종료됨 (작업 스케줄러 시간 제한, PC 종료·재부팅 등)" if not alive
                 else f"{last_prog:%H:%M} 이후 진행 기록 없음 — 멈춘 것으로 보임")
        left = [label for name, label in STEPS
                if name not in (st.get("completed_steps") or [])]
        return "\n".join([
            f"❌ 이직 공고 배치 오류 — {head}",
            f"  • 시작 {start:%H:%M} → {last_prog:%H:%M}에 멈춤",
            f"  • 멈춘 단계: {step_txt}, {_step_progress(st)}",
            f"  • 원인: {cause}",
            f"  • 처리 안 된 단계: {' · '.join(left)}",
            "  → 로그: ~/jobhunt-data/logs/jobhunt.log",
        ])
    step_start = _ts(st.get("step_started_at")) or start
    return "\n".join([
        f"⏳ 이직 공고 배치 진행 중 — {head}",
        f"  • 시작 {start:%H:%M} → 경과 {_dur(start, now)}",
        f"  • 현재 단계: {step_txt}  ← {_steps_line(st)}",
        f"  • 이 단계 진행: {_step_progress(st)}, 이 단계에서 {_dur(step_start, now)}째",
        f"  • 마지막 진행 기록: {last_prog:%H:%M}",
        "  → 끝나면 완료 알림이 따로 갈 거예요.",
    ])


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
    forms: dict[str, Counter] = {}   # 정규화 키 → 원 표기별 빈도 (출력은 가장 많이 쓰인 표기)

    def labels(raw: str | None) -> set[str]:
        out = set()
        for s in json.loads(raw or "[]"):
            if not s.strip():
                continue
            key = _label_key(s)
            forms.setdefault(key, Counter())[s.strip()] += 1
            out.add(key)
        return out

    for r in target:
        req.update(labels(r["required_skills"]))
        pref.update(labels(r["preferred_skills"]))
        gaps.update(labels(r["gaps"]))
    display = {k: c.most_common(1)[0][0] for k, c in forms.items()}
    roles = Counter(r["role_type"] for r in rows)
    eng = sum(r["english_required"] or 0 for r in target)

    def table(c: Counter, n: int) -> str:
        total = len(target) or 1
        return "\n".join(f"  {display[k]:<28s} {v:>4}  ({v / total:>4.0%})" for k, v in c.most_common(n))

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
