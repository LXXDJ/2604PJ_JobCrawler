"""step4 이직용 파이프라인 CLI.

사용:
  python -m scripts.jobhunt.run collect              # 원티드·잡코리아·사람인 수집 + 1차 필터 + 중복 묶기 + 상세
  python -m scripts.jobhunt.run collect --pages 2    # 앞 2페이지만 (테스트용, 마감처리 생략)
  python -m scripts.jobhunt.run collect --source saramin   # 특정 사이트만
  python -m scripts.jobhunt.run extract --max 60     # LLM 구조화 추출 + 적합도 (claude -p, 구독)
  python -m scripts.jobhunt.run extract --refit 50 --max 1000   # 경력·목표 수정 후 적합도 50+ 공고 재분석
  python -m scripts.jobhunt.run skills [--all]       # 기술 표기 분류 + 적합도 재계산 (보유 기술만 바꿨을 땐 이것만)
  python -m scripts.jobhunt.run skills --show has    # 분류표 보기 / --pin "Azure=partial" 로 직접 고정
  python -m scripts.jobhunt.run notify [--dry-run]   # 고적합 신규 공고 Slack (JOBHUNT_SLACK_WEBHOOK_URL)
  python -m scripts.jobhunt.run report               # 요구 기술 / 내 갭 빈도 리포트
  python -m scripts.jobhunt.run stats                # 수집 현황
  python -m scripts.jobhunt.run dashboard [--open]   # 로컬 HTML 대시보드 생성 (~/jobhunt-data/jobhunt_dashboard.html)
  python -m scripts.jobhunt.run serve [--port 8787]  # 대시보드 로컬 서버 (요청마다 DB 최신 반영)
  python -m scripts.jobhunt.run companies            # 회사 태그(인원·연봉 백분위·투자 등) 갱신
  python -m scripts.jobhunt.run ocr --max 40         # 이미지 통짜 공고 → 텍스트 (claude -p + Read)
  python -m scripts.jobhunt.run career --max 80      # 커리어 관점 포지션 평가 (추천도용, claude -p)
  python -m scripts.jobhunt.run daily [--max N]      # 매일 배치(00:00, 기본 상한 없음): collect → companies → ocr → extract → skills → career → dashboard → 완료 알림
  python -m scripts.jobhunt.run check [--dry-run]    # 08:00 점검: 배치가 안 끝났으면 진행 중 / 오류 / 미실행 Slack 알림

작업 스케줄러에서는 pythonw.exe 로 실행 → 출력은 ~/jobhunt-data/logs/jobhunt.log 로 간다.
개인 데이터(DB·대시보드·로그)는 ~/jobhunt-data (환경변수 JOBHUNT_DATA_DIR 로 변경 가능).
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from jobhunt.db import LOG_DIR

if sys.stdout is None:   # pythonw (작업 스케줄러) — 콘솔이 없으니 로그 파일로
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(LOG_DIR / "jobhunt.log", "a", encoding="utf-8", buffering=1)
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from jobhunt import batch_status, dashboard, fit, server
from jobhunt.career import evaluate
from jobhunt.collect import SOURCES, collect, refresh_companies
from jobhunt.db import get_conn
from jobhunt.extract import MODEL as EXTRACT_MODEL
from jobhunt.extract import extract
from jobhunt.ocr import ocr as run_ocr
from jobhunt.report import (HIGH_FIT, daily_check_message, daily_done_message, mark_notified, market_report,
                            notify_new, send)


def cmd_collect(args) -> None:
    t = time.time()
    srcs = [s for s in SOURCES if not args.source or s.SOURCE in args.source]
    rep = collect(max_pages=args.pages, sources=srcs)
    for name, st in rep.per_source.items():
        print(f"[collect:{name}] seen={st['seen']} new={st['new']} new_matched={st['new_matched']} "
              f"closed={st['closed']}")
    print(f"[collect] expired={rep.closed_expired} duplicates={rep.duplicates} "
          f"details={rep.details_fetched} errors={len(rep.errors)} ({time.time() - t:.0f}s)")
    for e in rep.errors[:5]:
        print(f"  ! {e}")
    return rep


def _all_failed(rep) -> None:
    """LLM 단계에서 처리 0건 + 오류만 있으면 (구독 한도·로그인 만료 등) 단계 실패로 본다."""
    if rep.errors and not rep.processed:
        raise RuntimeError(f"LLM 호출 {len(rep.errors)}건 모두 실패: {rep.errors[0][:200]}")


def cmd_extract(args) -> None:
    t = time.time()
    rep = extract(max_postings=args.max, model=args.model, refit_min=getattr(args, "refit", None))
    print(f"[extract] processed={rep.processed} rechecked={rep.rechecked} calls={rep.calls} "
          f"errors={len(rep.errors)} ({time.time() - t:.0f}s)")
    for e in rep.errors[:5]:
        print(f"  ! {e}")
    _all_failed(rep)
    if getattr(args, "skills", False):   # 단독 실행 시 새 기술 표기 분류 + 점수 재계산까지 (daily 는 별도 단계)
        cmd_skills(argparse.Namespace(all=False, model="sonnet", show=None, pin=None))


def cmd_skills(args) -> None:
    """기술 표기 분류(새 표기 / 보유 기술 변경 시 전체) → 모든 공고 fit_score·gaps 재계산."""
    with get_conn() as conn:
        if args.pin:
            label, _, verdict = args.pin.rpartition("=")
            if verdict not in ("has", "partial", "none", "na", "auto"):
                raise SystemExit("--pin 형식: '라벨=has|partial|none|na|auto'")
            fit.pin(conn, label, verdict)
            print(f"[skills] 고정: {label} = {verdict}")
        if args.show:
            for r in conn.execute(
                    "SELECT label, pinned FROM skill_labels WHERE verdict=? ORDER BY label COLLATE NOCASE",
                    (args.show,)):
                print(f"  {r['label']}{' (고정)' if r['pinned'] else ''}")
            return
        t = time.time()
        rep = fit.classify(conn, model=args.model, force=args.all)
        rep.rescored = fit.rescore(conn)
        print(f"[skills] labels={rep.labels} pending={rep.pending} processed={rep.processed} "
              f"direct={rep.direct} calls={rep.calls} rescored={rep.rescored} "
              f"errors={len(rep.errors)} ({time.time() - t:.0f}s)")
        for e in rep.errors[:5]:
            print(f"  ! {e}")
        if rep.errors and not rep.processed:
            raise RuntimeError(f"LLM 호출 {len(rep.errors)}건 모두 실패: {rep.errors[0][:200]}")


def cmd_companies(args) -> None:
    t = time.time()
    n, errors = refresh_companies(max_age_days=args.max_age)
    print(f"[companies] refreshed={n} errors={len(errors)} ({time.time() - t:.0f}s)")
    for e in errors[:5]:
        print(f"  ! {e}")


def cmd_ocr(args) -> None:
    t = time.time()
    rep = run_ocr(max_postings=args.max, model=args.model)
    print(f"[ocr] processed={rep.processed} no_image={rep.no_image} download_failed={rep.download_failed} "
          f"errors={len(rep.errors)} ({time.time() - t:.0f}s)")
    for e in rep.errors[:5]:
        print(f"  ! {e}")
    _all_failed(rep)


def cmd_career(args) -> None:
    t = time.time()
    rep = evaluate(max_postings=args.max, model=args.model)
    print(f"[career] processed={rep.processed} calls={rep.calls} "
          f"errors={len(rep.errors)} ({time.time() - t:.0f}s)")
    for e in rep.errors[:5]:
        print(f"  ! {e}")
    _all_failed(rep)


def cmd_notify(args) -> None:
    n, msg = notify_new(min_score=args.min_score, dry_run=args.dry_run)
    print(msg if args.dry_run else f"[notify] sent={n}")


def cmd_report(args) -> None:
    print(market_report(fit_min=args.fit_min))


def cmd_stats(args) -> None:
    with get_conn() as conn:
        for label, sql in [
            ("전체 공고", "SELECT COUNT(*) FROM postings"),
            ("open", "SELECT COUNT(*) FROM postings WHERE status='open'"),
            ("1차 매칭 (open)", "SELECT COUNT(*) FROM postings WHERE matched=1 AND status='open'"),
            ("상세 수집됨", "SELECT COUNT(*) FROM postings WHERE detail_fetched_at IS NOT NULL"),
            ("LLM 분석됨", "SELECT COUNT(*) FROM insights"),
        ]:
            print(f"{label:<16s} {conn.execute(sql).fetchone()[0]:>6,}")
        print("\n매칭 사유 TOP")
        for r in conn.execute(
            """SELECT match_reason, COUNT(*) n FROM postings WHERE matched=1 AND status='open'
               GROUP BY match_reason ORDER BY n DESC LIMIT 10"""
        ):
            print(f"  {r['match_reason']:<24s} {r['n']:>5}")


def cmd_dashboard(args) -> None:
    path = dashboard.build()
    print(f"[dashboard] {path}")
    if args.open:
        import webbrowser
        webbrowser.open(path.as_uri())


def cmd_serve(args) -> None:
    print(f"[serve] http://127.0.0.1:{args.port}/")
    server.serve(args.port)


def _collect_step() -> None:
    rep = cmd_collect(argparse.Namespace(pages=None, source=None))
    if not rep.per_source:
        raise RuntimeError(f"모든 사이트 목록 수집 실패: {rep.errors[0] if rep.errors else '-'}")
    batch_status.update(collect={
        "new": sum(s["new"] for s in rep.per_source.values()),
        "new_matched": sum(s["new_matched"] for s in rep.per_source.values()),
        "closed": sum(s["closed"] for s in rep.per_source.values()) + rep.closed_expired,
    })


def _notify_step(min_score: int) -> None:
    """앞 단계가 모두 성공했을 때만 완료 알림. 실패가 있으면 오전 8시 점검이 오류 알림을 보낸다."""
    st = batch_status.load()
    if st.get("failures"):
        print("[notify] 실패한 단계가 있어 완료 알림 생략 — 08:00 점검에서 오류 알림")
        return
    msg, ids = daily_done_message(st, min_score=min_score)
    send(msg)
    mark_notified(ids)
    print(f"[notify] 완료 알림 전송 (고적합 {len(ids)}건)")


def cmd_daily(args) -> None:
    """한 단계가 실패해도 다음 단계는 진행 (예: 수집 실패해도 기존 미분석분 extract).
    진행 상태는 ~/jobhunt-data/logs/daily_status.json 에 남겨 오전 8시 점검(check)이 읽는다."""
    print(f"\n===== daily {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
    batch_status.start()
    steps = {
        "collect": _collect_step,
        "companies": lambda: cmd_companies(argparse.Namespace(max_age=30)),
        "ocr": lambda: cmd_ocr(argparse.Namespace(max=args.max, model=args.model)),
        "extract": lambda: cmd_extract(argparse.Namespace(max=args.max, model=EXTRACT_MODEL)),   # 정확도 우선 sonnet
        "skills": lambda: cmd_skills(argparse.Namespace(all=False, model="sonnet", show=None, pin=None)),
        "career": lambda: cmd_career(argparse.Namespace(max=args.max, model=args.model)),
        "dashboard": lambda: cmd_dashboard(argparse.Namespace(open=False)),
        "notify": lambda: _notify_step(args.min_score),
    }
    for name, _ in batch_status.STEPS:
        batch_status.step(name)
        try:
            steps[name]()
            batch_status.step_ok(name)
        except Exception as e:
            print(f"[{name}] 실패: {type(e).__name__}: {e}")
            batch_status.step_failed(name, f"{type(e).__name__}: {e}")
    batch_status.finish()
    print(f"===== done {time.strftime('%H:%M:%S')} =====")


def cmd_check(args) -> None:
    """오전 8시 점검 — 오늘 배치가 정상 완료되지 않았으면 진행 중 / 오류 / 미실행 알림."""
    msg = daily_check_message()
    if not msg:
        print("[check] 오늘 배치 정상 완료 — 알림 없음")
        return
    if args.dry_run:
        print(msg)
        return
    send(msg)
    print(f"[check] 점검 알림 전송: {msg.splitlines()[0]}")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect")
    p.add_argument("--pages", type=int, default=None, help="앞 N 페이지만 (테스트용)")
    p.add_argument("--source", nargs="*", choices=[s.SOURCE for s in SOURCES], help="이 사이트만 (기본 전체)")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("extract")
    p.add_argument("--max", type=int, default=60, help="이번 회차 최대 분석 건수 (구독 한도 보호)")
    p.add_argument("--model", default=EXTRACT_MODEL, help="claude 모델 alias (기본 sonnet — 업무 범위 판단 정확도)")
    p.add_argument("--refit", type=int, default=None, metavar="MIN",
                   help="경력·목표 수정 후 재분석: 적합도 MIN 이상 + 현재 프로필 버전으로 분석 안 된 것만")
    p.set_defaults(func=cmd_extract, skills=True)

    p = sub.add_parser("skills")
    p.add_argument("--all", action="store_true", help="고정값 외 전체 재분류 (기본: 새 표기 + 보유 기술 변경분)")
    p.add_argument("--model", default="sonnet", help="분류 모델 (호출 수가 적어 sonnet)")
    p.add_argument("--show", choices=["has", "partial", "none", "na"], help="해당 분류의 표기 목록만 출력 (na=기술 아님)")
    p.add_argument("--pin", metavar="LABEL=VERDICT", help="분류 직접 고정 (has|partial|none|na, auto=고정 해제)")
    p.set_defaults(func=cmd_skills)

    p = sub.add_parser("companies")
    p.add_argument("--max-age", type=int, default=30, help="이 일수보다 오래된 회사 정보만 다시 받음")
    p.set_defaults(func=cmd_companies)

    p = sub.add_parser("ocr")
    p.add_argument("--max", type=int, default=40, help="이번 회차 최대 OCR 공고 수 (구독 한도 보호)")
    p.add_argument("--model", default="haiku")
    p.set_defaults(func=cmd_ocr)

    p = sub.add_parser("career")
    p.add_argument("--max", type=int, default=80, help="이번 회차 최대 평가 건수 (구독 한도 보호)")
    p.add_argument("--model", default="haiku")
    p.set_defaults(func=cmd_career)

    p = sub.add_parser("notify")
    p.add_argument("--min-score", type=int, default=HIGH_FIT)
    p.add_argument("--dry-run", action="store_true", help="보내지 않고 메시지만 출력")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("report")
    p.add_argument("--fit-min", type=int, default=60, help="집계 대상 적합도 하한")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("stats")
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("dashboard")
    p.add_argument("--open", action="store_true", help="생성 후 브라우저로 열기")
    p.set_defaults(func=cmd_dashboard)

    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=server.DEFAULT_PORT)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("daily")
    # 새벽 배치라 기본은 상한 없음 (2026-11-04 — 300건 상한에 걸려 신규 공고 분석이 하루씩 밀림)
    p.add_argument("--max", type=int, default=-1, help="단계별(OCR·분석·커리어) 최대 건수, -1 = 상한 없음 (SQLite LIMIT -1)")
    p.add_argument("--model", default="haiku", help="OCR·커리어 평가 모델 (공고 분석은 항상 sonnet)")
    p.add_argument("--min-score", type=int, default=HIGH_FIT, help="완료 알림에 올릴 고적합 공고 기준")
    p.set_defaults(func=cmd_daily)

    p = sub.add_parser("check")
    p.add_argument("--dry-run", action="store_true", help="보내지 않고 메시지만 출력")
    p.set_defaults(func=cmd_check)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
