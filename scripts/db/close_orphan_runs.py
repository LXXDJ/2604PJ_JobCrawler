"""ended_at IS NULL 인 crawl_runs 중 너무 오래된 (=죽은) run 을 'failed' 로 마감.

배치 프로세스가 강제 종료 (taskkill / 시스템 재시작) 되면 _finish_run 이 안 불려서
ended_at 이 NULL 로 남음. dashboard 는 그걸 진행 중으로 오해.

기본 임계: 30분. 그 이상 안 끝난 run 은 죽은 것으로 간주.
"""
import argparse
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.infra.db import get_conn


def main(threshold_minutes: int = 30) -> None:
    with get_conn() as c:
        # SQLite datetime: julianday 차이로 분 계산
        rows = c.execute(
            f"""
            SELECT id, site_id, started_at,
                   CAST((julianday('now') - julianday(started_at)) * 24 * 60 AS INTEGER) AS age_min
              FROM crawl_runs
             WHERE ended_at IS NULL
               AND (julianday('now') - julianday(started_at)) * 24 * 60 > {threshold_minutes}
            """
        ).fetchall()
        if not rows:
            print(f"no orphan runs older than {threshold_minutes} min")
            return
        for r in rows:
            print(f"  closing run#{r['id']} ({r['site_id']}, age {r['age_min']}min)")
        c.execute(
            f"""
            UPDATE crawl_runs
               SET ended_at = datetime('now'),
                   result = 'failed',
                   error = COALESCE(error, '') || ' [orphan: process killed before finish]'
             WHERE ended_at IS NULL
               AND (julianday('now') - julianday(started_at)) * 24 * 60 > {threshold_minutes}
            """
        )
        print(f"closed {len(rows)} orphan run(s)")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=int, default=30,
                    help="이 분 이상 안 끝난 run 을 failed 로 마감 (기본 30)")
    args = ap.parse_args()
    main(args.minutes)
