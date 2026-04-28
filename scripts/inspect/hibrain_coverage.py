"""hibrain 13 sources 별 coverage 검증.

crawl_runs 로 최근 4차 run 이력 뽑고, source (sub_menu) 별로:
  - 해당 run 의 new+updated 합 (수집 건수)
  - sites.json 의 해당 source list_url
을 정렬.

의심 패턴:
  - 조기종료(기존 30건 트리거) 로 뒷 페이지 놓친 경우
  - TIMEOUT 중단
  - 메뉴 이름에 '265건' 같은 숫자 포함 vs 실제 수집 건수 차이
"""
import io
import json
import re
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent.parent
DB = sqlite3.connect(ROOT / "data" / "jobs.db")
cur = DB.cursor()

# sites.json 에서 hibrain sources 목록
with open(ROOT / "data" / "sites.json", encoding="utf-8") as f:
    sites = json.load(f)
hibrain = next(e for e in sites if e["site_id"] == "hibrain")
sources = hibrain.get("sources") or []
print(f"hibrain 등록 sources: {len(sources)}개\n")

for i, s in enumerate(sources, 1):
    name = s.get("menu_name", "?")
    url = (s.get("source") or {}).get("list_url", "?")
    # 메뉴명에 '123건' 같은 패턴이 있으면 추출
    m = re.search(r"(\d+)\s*건", name)
    hint = f"  (메뉴명 숫자={m.group(1)})" if m else ""
    print(f"  {i:>2}. {name:<25} {url}{hint}")

# 최근 run 4회차 (run id 256부터 이번 돌림) — sub-source 단위로 기록됨
print("\n==== 최근 hibrain crawl_runs (4차 v4 추정, 13개 run/회) ====")
rows = cur.execute(
    "SELECT id, started_at, finished_at, new_count, updated_count, error "
    "FROM crawl_runs WHERE source='hibrain' ORDER BY id DESC LIMIT 60"
).fetchall()
for r in rows:
    id_, start, end, n, u, err = r
    dur = "?"
    if start and end:
        try:
            dur = f"{(datetime.fromisoformat(end)-datetime.fromisoformat(start)).total_seconds():.0f}s"
        except Exception:
            pass
    err_flag = f" ERR={err[:40]}" if err else ""
    print(f"  [{id_}] {start[:19]} → {end[:19] if end else '진행중':<19} {dur:>6} new={n:>4} upd={u:>4}{err_flag}")

# hibrain 전체 DB count
cnt = cur.execute("SELECT COUNT(*) FROM jobs WHERE source='hibrain'").fetchone()[0]
print(f"\nhibrain jobs DB 총합: {cnt}건")

# hibrain jobs link URL 에서 /categories/XXX/recruits/ 패턴으로 sub-source 역추적
print("\n==== sub-source 별 DB 수집량 (URL 카테고리 코드) ====")

# URL 패턴: /recruitment/categories/{axis}/categories/{code}/recruits/{id}
# axis: JOB / MJR / COMP / DGR  (JOB이 주, 나머지는 교차분류)
CODE_NAMES = {
    "RES": "연구원", "PDOC": "Post-Doc", "GOV": "공무원", "EMP": "직원",
    "EXP": "전문가", "MIL": "병역특례", "TPROF": "강사", "PROF": "유형별(전문직)",
    "MJR/ALL": "분류별(전공)", "COMP/ALL": "기관별", "DGR/ALL": "학력별",
}

import re
axis_code_count = {}
total = 0
for (link,) in cur.execute("SELECT link FROM jobs WHERE source='hibrain'"):
    m = re.search(r"/categories/([^/]+)/categories/([^/]+)/recruits/", link or "")
    if m:
        axis, code = m.group(1), m.group(2)
        key = f"{axis}/{code}" if axis != "JOB" else code
        axis_code_count[key] = axis_code_count.get(key, 0) + 1
    else:
        axis_code_count["OTHER"] = axis_code_count.get("OTHER", 0) + 1
    total += 1

for code, cnt in sorted(axis_code_count.items(), key=lambda x: -x[1]):
    label = CODE_NAMES.get(code, "?")
    print(f"  {code:<10} {label:<20} {cnt:>5}건")
print(f"  {'TOTAL':<10} {'':<20} {total:>5}건")
