"""공고 수집 — 리스트 전체 순회 → upsert → 1차 필터 → 매칭 공고만 상세 fetch."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from . import profile
from .db import get_conn
from .sources import wanted

SOURCE = "wanted"


@dataclass
class CollectReport:
    seen: int = 0
    new: int = 0
    new_matched: int = 0
    closed: int = 0
    details_fetched: int = 0
    detail_errors: list[str] = field(default_factory=list)


def _upsert(conn, it: wanted.ListItem) -> tuple[bool, bool]:
    """Returns (is_new, matched)."""
    ok, reason = profile.match(it.title, it.category_ids, it.years_from, it.years_to)
    row = conn.execute(
        "SELECT id FROM postings WHERE source=? AND external_id=?", (SOURCE, it.external_id)
    ).fetchone()
    if row:
        # 재오픈 공고도 open 으로 되돌림. matched 는 필터 규칙이 바뀌었을 수 있어 매번 재계산
        conn.execute(
            """UPDATE postings SET title=?, company=?, location=?, years_from=?, years_to=?,
                   due_date=?, category_ids=?, matched=?, match_reason=?,
                   status='open', closed_at=NULL, last_seen_at=datetime('now','localtime')
               WHERE id=?""",
            (it.title, it.company, it.location, it.years_from, it.years_to, it.due_date,
             json.dumps(it.category_ids), int(ok), reason, row["id"]),
        )
        return False, ok
    conn.execute(
        """INSERT INTO postings (source, external_id, url, title, company, company_industry, location,
               years_from, years_to, due_date, category_ids, matched, match_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (SOURCE, it.external_id, it.url, it.title, it.company, it.company_industry, it.location,
         it.years_from, it.years_to, it.due_date, json.dumps(it.category_ids), int(ok), reason),
    )
    return True, ok


def collect(*, max_pages: int | None = None, detail_sleep: float = 0.3) -> CollectReport:
    rep = CollectReport()
    seen_ids: set[str] = set()

    with get_conn() as conn:
        for it in wanted.iter_list(max_pages=max_pages):
            if it.external_id in seen_ids:   # 페이지 경계에서 같은 공고가 다시 나오는 경우
                continue
            seen_ids.add(it.external_id)
            is_new, ok = _upsert(conn, it)
            rep.new += is_new
            rep.new_matched += is_new and ok
        rep.seen = len(seen_ids)

        # 마감 처리 — 리스트를 끝까지 돌았을 때만. 부분 수집(max_pages)이면 건너뜀
        if max_pages is None:
            open_before = conn.execute(
                "SELECT COUNT(*) FROM postings WHERE source=? AND status='open'", (SOURCE,)
            ).fetchone()[0]
            # 안전장치: 이번에 본 공고가 기존 open 의 절반도 안 되면 API 이상으로 보고 마감 처리 안 함
            if rep.seen >= open_before * 0.5:
                conn.execute("CREATE TEMP TABLE seen_now (external_id TEXT PRIMARY KEY)")
                conn.executemany("INSERT INTO seen_now VALUES (?)", [(x,) for x in seen_ids])
                cur = conn.execute(
                    """UPDATE postings SET status='closed', closed_at=datetime('now','localtime')
                       WHERE source=? AND status='open'
                         AND external_id NOT IN (SELECT external_id FROM seen_now)""",
                    (SOURCE,),
                )
                rep.closed = cur.rowcount
                conn.execute("DROP TABLE seen_now")
        conn.commit()

        # 상세 — 매칭된 open 공고 중 아직 본문 없는 것만
        todo = conn.execute(
            """SELECT id, external_id FROM postings
               WHERE source=? AND matched=1 AND status='open' AND detail_fetched_at IS NULL""",
            (SOURCE,),
        ).fetchall()
        for row in todo:
            try:
                d = wanted.fetch_detail(row["external_id"])
            except Exception as e:  # noqa: BLE001
                rep.detail_errors.append(f"{row['external_id']}: {e}")
                continue
            conn.execute(
                """UPDATE postings SET intro=?, main_tasks=?, requirements=?, preferred=?, benefits=?,
                       skill_tags=?, detail_fetched_at=datetime('now','localtime')
                   WHERE id=?""",
                (d["intro"], d["main_tasks"], d["requirements"], d["preferred"], d["benefits"],
                 json.dumps(d["skill_tags"], ensure_ascii=False), row["id"]),
            )
            conn.commit()
            rep.details_fetched += 1
            time.sleep(detail_sleep)
    return rep
