"""일일 배치 CLI.

사용:
  python scripts/run_batch.py                   # active 모든 사이트
  python scripts/run_batch.py --site career     # 특정 사이트만
  python scripts/run_batch.py --no-detail       # 상세 fetch 생략 (빠름)
  python scripts/run_batch.py --no-slack        # 슬랙 알림 끄기 (default: 보냄)
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

from crawlers.batch.scheduler import run_batch, BatchReport
from crawlers.infra.notify import slack_notify


def _short_error(err: str | None) -> str:
    if not err:
        return ""
    s = err.splitlines()[0]
    return s[:120]


def _format_elapsed(seconds: float) -> str:
    total = int(round(seconds))
    h, rem = divmod(total, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}시간 {m}분 {s}초"
    if m:
        return f"{m}분 {s}초"
    return f"{s}초"


def _build_slack_message(rep: BatchReport, elapsed: float) -> str:
    header = (
        f":rotating_light: *크롤 완료*: 성공 {rep.succeeded} / 실패 {rep.failed} "
        f"(전체 {rep.total_sites}, {_format_elapsed(elapsed)})\n"
        f":new: 신규 공고 건수 {rep.inserted}\n"
        f"---"
    )
    lines = [header]
    failed = [sr for sr in rep.site_reports if not sr.success]
    succeeded = sorted(
        (sr for sr in rep.site_reports if sr.success),
        key=lambda s: (-s.inserted, s.display_name),
    )
    for sr in failed:
        warn = ""
        if sr.consecutive_failures >= 3:
            warn = f" (연속실패 {sr.consecutive_failures}회)"
        lines.append(
            f":x: {sr.display_name} — {_short_error(sr.error)}{warn}"
        )
    for sr in succeeded:
        lines.append(
            f":white_check_mark: {sr.display_name} — "
            f"신규 {sr.inserted}, 누적 {sr.jobs_total}"
        )

    # 메시지 너무 길면 일부 잘라냄 (슬랙 4000자 제한 안전마진)
    full = "\n".join(lines)
    if len(full) > 3500:
        kept = lines[:60]
        kept.append(f"... 외 {len(lines)-60}개 (이하 생략)")
        full = "\n".join(kept)
    return full


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", action="append", default=None,
                    help="크롤할 site_id (여러 번 지정 가능). 생략 시 active 전부.")
    ap.add_argument("--no-detail", action="store_true",
                    help="상세 페이지 fetch 생략 (목록만)")
    ap.add_argument("--interval", type=float, default=1.0,
                    help="사이트 간 휴식 초")
    ap.add_argument("--no-slack", action="store_true",
                    help="슬랙 알림 보내지 않음")
    args = ap.parse_args()

    # 시작 직전 — 좀비 run 자동 청소 (이전 배치가 강제 종료되어 ended_at NULL 로 남은 것)
    from scripts.db.close_orphan_runs import main as _close_orphans
    try:
        _close_orphans(threshold_minutes=30)
    except Exception as e:  # noqa: BLE001
        print(f"[warn] close_orphan_runs failed: {e}", flush=True)

    started = time.time()
    rep = run_batch(
        site_ids=args.site,
        fetch_details=not args.no_detail,
        interval_sec=args.interval,
    )
    elapsed = time.time() - started

    print(f"\n=== BATCH SUMMARY ===")
    print(f"sites: {rep.total_sites}  ok={rep.succeeded}  fail={rep.failed}")
    print(f"jobs:  inserted={rep.inserted}  updated={rep.updated}  closed={rep.closed}")

    print(f"\n=== PER SITE ===")
    for sr in rep.site_reports:
        flag = "OK " if sr.success else "FAIL"
        print(f"[{flag}] {sr.site_id:<24s} "
              f"sources={sr.sources_crawled} rows={sr.rows_seen} "
              f"+{sr.inserted} ~{sr.updated} ={sr.unchanged} closed={sr.closed} "
              f"detail_err={sr.detail_errors}")
        if sr.error:
            print(f"        error: {sr.error}")

    if not args.no_slack:
        slack_notify(_build_slack_message(rep, elapsed))


if __name__ == "__main__":
    main()
