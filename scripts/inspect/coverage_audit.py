"""1차 검증: crawl_runs + jobs + sites.json 조합으로 각 사이트 coverage 점검.

출력:
  - 각 사이트 등록 sources 수 vs 최근 run 수 vs 성공 run 수
  - jobs DB count vs 최근 "대표 run" 누적 new+updated 비교
  - error 있는 run 목록
  - 마지막 크롤 시각 (신선도)
"""
import io
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent.parent
DB = sqlite3.connect(ROOT / "data" / "jobs.db")
cur = DB.cursor()

with open(ROOT / "data" / "sites.json", encoding="utf-8") as f:
    sites = json.load(f)

# 1) 각 site_id 의 등록 sources 개수
site_sources = {}
for e in sites:
    site_id = e["site_id"]
    srcs = e.get("sources") or []
    site_sources[site_id] = len(srcs) if srcs else 1  # v1 legacy = 1

# 2) 최근 24시간 run 통계 / 전체 run 통계 분리
cutoff_24h = (datetime.utcnow() - timedelta(hours=24)).isoformat()

runs_recent = defaultdict(list)   # 최근 24h 내 run
runs_latest_ts = {}                # 마지막 run 시각
run_errors = defaultdict(int)
run_success = defaultdict(int)
run_total = defaultdict(int)
run_sum_new = defaultdict(int)
run_sum_upd = defaultdict(int)

for row in cur.execute("SELECT source, started_at, finished_at, new_count, updated_count, error FROM crawl_runs"):
    source, started, finished, n, u, err = row
    run_total[source] += 1
    if err:
        run_errors[source] += 1
    if finished and not err:
        run_success[source] += 1
    if started > cutoff_24h:
        runs_recent[source].append((started, finished, n or 0, u or 0, err))
    if source not in runs_latest_ts or (started and started > runs_latest_ts[source]):
        runs_latest_ts[source] = started
    runs_recent  # noqa
    run_sum_new[source] += n or 0
    run_sum_upd[source] += u or 0

# 3) jobs DB count per source
db_counts = {}
for row in cur.execute("SELECT source, COUNT(*) FROM jobs GROUP BY source"):
    db_counts[row[0]] = row[1]

# ------- 리포트 -------
print(f"{'site_id':<16} {'srcs':>4} {'runs':>5} {'ok':>4} {'err':>4} {'db_cnt':>7} {'last_run':<20} {'24h_ok':>6} {'24h_sumN':>9}")
print("-" * 95)

all_ids = sorted(set(list(site_sources.keys()) + list(db_counts.keys())))

issues = []
for site_id in all_ids:
    srcs_reg = site_sources.get(site_id, 0)
    total = run_total.get(site_id, 0)
    success = run_success.get(site_id, 0)
    errs = run_errors.get(site_id, 0)
    db_cnt = db_counts.get(site_id, 0)
    last = runs_latest_ts.get(site_id, "") or ""
    last_short = last[:19] if last else "(없음)"
    recent24 = runs_recent.get(site_id, [])
    recent24_ok = sum(1 for _, f, _, _, e in recent24 if f and not e)
    recent24_sum_n = sum(n for _, _, n, _, _ in recent24)

    fresh_warn = ""
    if not last:
        fresh_warn = "  ⚠ never crawled"
    elif last < cutoff_24h:
        # 24시간 이상 안 돌았음
        try:
            dt = datetime.fromisoformat(last.replace("Z",""))
            hrs = (datetime.utcnow() - dt).total_seconds() / 3600
            fresh_warn = f"  ⚠ {hrs:.1f}h ago"
        except Exception:
            fresh_warn = "  ⚠ stale"

    print(f"{site_id:<16} {srcs_reg:>4} {total:>5} {success:>4} {errs:>4} {db_cnt:>7} {last_short:<20} {recent24_ok:>6} {recent24_sum_n:>9}{fresh_warn}")

    # 문제 감지:
    if db_cnt == 0:
        issues.append(f"  [DB=0] {site_id} — 한 건도 수집 안됨")
    if srcs_reg > 0 and total == 0:
        issues.append(f"  [미실행] {site_id} — sources={srcs_reg} 있으나 run 이력 없음")
    if total > 0 and success == 0:
        issues.append(f"  [전부실패] {site_id} — runs={total} 모두 실패/미완")
    if srcs_reg > 1 and recent24 and recent24_ok < srcs_reg and last > cutoff_24h:
        issues.append(f"  [부분성공] {site_id} — sources={srcs_reg} 중 24h 내 성공 {recent24_ok}")

print("\n==== 이슈 요약 ====")
if not issues:
    print("  ✅ 명백한 이슈 없음")
else:
    for it in issues:
        print(it)

# 에러 run 상세
print("\n==== 최근 에러 run (최근 10개) ====")
n = 0
for row in cur.execute(
    "SELECT source, started_at, error FROM crawl_runs WHERE error IS NOT NULL ORDER BY id DESC LIMIT 10"
):
    print(f"  [{row[1][:19]}] {row[0]}: {row[2][:100] if row[2] else ''}")
    n += 1
if n == 0:
    print("  (없음)")
