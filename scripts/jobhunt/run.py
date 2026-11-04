"""step4 이직용 파이프라인 CLI.

사용:
  python -m scripts.jobhunt.run collect              # 원티드·잡코리아·사람인 수집 + 1차 필터 + 중복 묶기 + 상세
  python -m scripts.jobhunt.run collect --pages 2    # 앞 2페이지만 (테스트용, 마감처리 생략)
  python -m scripts.jobhunt.run collect --source saramin   # 특정 사이트만
  python -m scripts.jobhunt.run extract --max 60     # LLM 구조화 추출 + 적합도 (claude -p, 구독)
  python -m scripts.jobhunt.run notify [--dry-run]   # 고적합 신규 공고 Slack (JOBHUNT_SLACK_WEBHOOK_URL)
  python -m scripts.jobhunt.run report               # 요구 기술 / 내 갭 빈도 리포트
  python -m scripts.jobhunt.run stats                # 수집 현황
  python -m scripts.jobhunt.run dashboard [--open]   # 로컬 HTML 대시보드 생성 (data/jobhunt_dashboard.html)
  python -m scripts.jobhunt.run serve [--port 8787]  # 대시보드 로컬 서버 (요청마다 DB 최신 반영)
  python -m scripts.jobhunt.run companies            # 회사 태그(인원·연봉 백분위·투자 등) 갱신
  python -m scripts.jobhunt.run ocr --max 40         # 이미지 통짜 공고 → 텍스트 (claude -p + Read)
  python -m scripts.jobhunt.run career --max 80      # 커리어 관점 포지션 평가 (추천도용, claude -p)
  python -m scripts.jobhunt.run daily [--max 300]    # 매일 배치: collect → companies → ocr → extract → career → notify → dashboard

작업 스케줄러에서는 pythonw.exe 로 실행 → 출력은 data/logs/jobhunt.log 로 간다.
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
if sys.stdout is None:   # pythonw (작업 스케줄러) — 콘솔이 없으니 로그 파일로
    _log_dir = ROOT / "data" / "logs"
    _log_dir.mkdir(parents=True, exist_ok=True)
    sys.stdout = sys.stderr = open(_log_dir / "jobhunt.log", "a", encoding="utf-8", buffering=1)
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from jobhunt import dashboard, server
from jobhunt.career import evaluate
from jobhunt.collect import SOURCES, collect, refresh_companies
from jobhunt.db import get_conn
from jobhunt.extract import extract
from jobhunt.ocr import ocr as run_ocr
from jobhunt.report import WEBHOOK, market_report, notify_new


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


def cmd_extract(args) -> None:
    t = time.time()
    rep = extract(max_postings=args.max, model=args.model)
    print(f"[extract] processed={rep.processed} calls={rep.calls} "
          f"errors={len(rep.errors)} ({time.time() - t:.0f}s)")
    for e in rep.errors[:5]:
        print(f"  ! {e}")


def cmd_companies(args) -> None:
    t = time.time()
    n, errors = refresh_companies(max_age_days=args.max_age)
    print(f"[companies] refreshed={n} errors={len(errors)} ({time.time() - t:.0f}s)")
    for e in errors[:5]:
        print(f"  ! {e}")


def cmd_ocr(args) -> None:
    t = time.time()
    rep = run_ocr(max_postings=args.max, model=args.model)
    print(f"[ocr] processed={rep.processed} no_image={rep.no_image} "
          f"errors={len(rep.errors)} ({time.time() - t:.0f}s)")
    for e in rep.errors[:5]:
        print(f"  ! {e}")


def cmd_career(args) -> None:
    t = time.time()
    rep = evaluate(max_postings=args.max, model=args.model)
    print(f"[career] processed={rep.processed} calls={rep.calls} "
          f"errors={len(rep.errors)} ({time.time() - t:.0f}s)")
    for e in rep.errors[:5]:
        print(f"  ! {e}")


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


def cmd_daily(args) -> None:
    """한 단계가 실패해도 다음 단계는 진행 (예: 수집 실패해도 기존 미분석분 extract)."""
    print(f"\n===== daily {time.strftime('%Y-%m-%d %H:%M:%S')} =====")
    steps = [
        ("collect", lambda: cmd_collect(argparse.Namespace(pages=None, source=None))),
        ("companies", lambda: cmd_companies(argparse.Namespace(max_age=30))),
        ("ocr", lambda: cmd_ocr(argparse.Namespace(max=60, model=args.model))),
        ("extract", lambda: cmd_extract(argparse.Namespace(max=args.max, model=args.model))),
        ("career", lambda: cmd_career(argparse.Namespace(max=args.max, model=args.model))),
        ("notify", lambda: cmd_notify(argparse.Namespace(min_score=args.min_score, dry_run=False))
            if WEBHOOK else print("[notify] JOBHUNT_SLACK_WEBHOOK_URL 없음 — 건너뜀")),
        ("dashboard", lambda: cmd_dashboard(argparse.Namespace(open=False))),
    ]
    for name, fn in steps:
        try:
            fn()
        except Exception as e:
            print(f"[{name}] 실패: {type(e).__name__}: {e}")
    print(f"===== done {time.strftime('%H:%M:%S')} =====")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect")
    p.add_argument("--pages", type=int, default=None, help="앞 N 페이지만 (테스트용)")
    p.add_argument("--source", nargs="*", choices=[s.SOURCE for s in SOURCES], help="이 사이트만 (기본 전체)")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("extract")
    p.add_argument("--max", type=int, default=60, help="이번 회차 최대 분석 건수 (구독 한도 보호)")
    p.add_argument("--model", default="haiku", help="claude 모델 alias (haiku / sonnet / opus)")
    p.set_defaults(func=cmd_extract)

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
    p.add_argument("--min-score", type=int, default=65)
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
    p.add_argument("--max", type=int, default=300, help="이번 회차 최대 분석 건수 (남으면 다음 날 이어서)")
    p.add_argument("--model", default="haiku")
    p.add_argument("--min-score", type=int, default=65)
    p.set_defaults(func=cmd_daily)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
