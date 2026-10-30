"""step4 이직용 파이프라인 CLI.

사용:
  python -m scripts.jobhunt.run collect              # 원티드 수집 + 1차 필터 + 상세
  python -m scripts.jobhunt.run collect --pages 2    # 앞 2페이지만 (테스트용, 마감처리 생략)
  python -m scripts.jobhunt.run extract --max 60     # LLM 구조화 추출 + 적합도 (claude -p, 구독)
  python -m scripts.jobhunt.run notify [--dry-run]   # 고적합 신규 공고 Slack (JOBHUNT_SLACK_WEBHOOK_URL)
  python -m scripts.jobhunt.run report               # 요구 기술 / 내 갭 빈도 리포트
  python -m scripts.jobhunt.run stats                # 수집 현황
"""
from __future__ import annotations

import argparse
import io
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from jobhunt.collect import collect
from jobhunt.db import get_conn
from jobhunt.extract import extract
from jobhunt.report import market_report, notify_new


def cmd_collect(args) -> None:
    t = time.time()
    rep = collect(max_pages=args.pages)
    print(f"[collect] seen={rep.seen} new={rep.new} new_matched={rep.new_matched} "
          f"closed={rep.closed} details={rep.details_fetched} "
          f"detail_errors={len(rep.detail_errors)} ({time.time() - t:.0f}s)")
    for e in rep.detail_errors[:5]:
        print(f"  ! {e}")


def cmd_extract(args) -> None:
    t = time.time()
    rep = extract(max_postings=args.max, model=args.model)
    print(f"[extract] processed={rep.processed} calls={rep.calls} "
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


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("collect")
    p.add_argument("--pages", type=int, default=None, help="앞 N 페이지만 (테스트용)")
    p.set_defaults(func=cmd_collect)

    p = sub.add_parser("extract")
    p.add_argument("--max", type=int, default=60, help="이번 회차 최대 분석 건수 (구독 한도 보호)")
    p.add_argument("--model", default="haiku", help="claude 모델 alias (haiku / sonnet / opus)")
    p.set_defaults(func=cmd_extract)

    p = sub.add_parser("notify")
    p.add_argument("--min-score", type=int, default=65)
    p.add_argument("--dry-run", action="store_true", help="보내지 않고 메시지만 출력")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("report")
    p.add_argument("--fit-min", type=int, default=60, help="집계 대상 적합도 하한")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("stats")
    p.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
