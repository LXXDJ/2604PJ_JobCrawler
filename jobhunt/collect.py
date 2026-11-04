"""공고 수집 — 사이트별 리스트 → upsert → 1차 필터 → 사이트 간 중복 묶기 → 마감 처리 → 매칭 공고만 상세."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field

from . import batch_status, profile
from .db import get_conn
from .sources import jobkorea, saramin, wanted

# 중복일 때 대표로 남길 우선순위 (본문이 섹션별로 구조화된 원티드가 LLM 분석에 가장 좋음)
SOURCES = [wanted, jobkorea, saramin]
_PRIORITY = {s.SOURCE: i for i, s in enumerate(SOURCES)}

STALE_DAYS = 30     # 검색형 사이트의 상시 공고: 마지막으로 본 지 이만큼 지나면 마감으로 봄


@dataclass
class CollectReport:
    per_source: dict = field(default_factory=dict)   # source → {seen, new, new_matched, closed}
    closed_expired: int = 0
    duplicates: int = 0
    details_fetched: int = 0
    errors: list[str] = field(default_factory=list)


def _upsert(conn, source: str, it) -> tuple[bool, bool]:
    """Returns (is_new, matched)."""
    ok, reason = profile.match(it.title, it.ai_category, it.years_from, it.years_to)
    cats = json.dumps(it.category_ids, ensure_ascii=False)
    row = conn.execute(
        "SELECT id FROM postings WHERE source=? AND external_id=?", (source, it.external_id)
    ).fetchone()
    if row:
        # 재오픈 공고도 open 으로 되돌림. matched 는 필터 규칙이 바뀌었을 수 있어 매번 재계산
        conn.execute(
            """UPDATE postings SET title=?, company=?, company_id=?, location=?, years_from=?, years_to=?,
                   due_date=?, category_ids=?, matched=?, match_reason=?,
                   status='open', closed_at=NULL, last_seen_at=datetime('now','localtime')
               WHERE id=?""",
            (it.title, it.company, it.company_id, it.location, it.years_from, it.years_to, it.due_date,
             cats, int(ok), reason, row["id"]),
        )
        return False, ok
    conn.execute(
        """INSERT INTO postings (source, external_id, url, title, company, company_id, company_industry,
               location, years_from, years_to, due_date, category_ids, matched, match_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (source, it.external_id, it.url, it.title, it.company, it.company_id, it.company_industry,
         it.location, it.years_from, it.years_to, it.due_date, cats, int(ok), reason),
    )
    return True, ok


def _collect_list(conn, src, max_pages: int | None) -> dict:
    stat = {"seen": 0, "new": 0, "new_matched": 0, "closed": 0}
    known = {r["external_id"] for r in conn.execute(
        "SELECT external_id FROM postings WHERE source=? AND status='open'", (src.SOURCE,))}
    seen_ids: set[str] = set()
    for it in src.iter_list(max_pages=max_pages, known_ids=known):
        if it.external_id in seen_ids:   # 페이지 경계·여러 검색어에서 같은 공고가 다시 나오는 경우
            continue
        seen_ids.add(it.external_id)
        is_new, ok = _upsert(conn, src.SOURCE, it)
        stat["new"] += is_new
        stat["new_matched"] += is_new and ok
        if len(seen_ids) % 100 == 0:   # 검색형은 수 분 걸려서 — 쓰기 잠금을 오래 잡지 않게 (동시에 도는 분석 배치 보호)
            conn.commit()
    stat["seen"] = len(seen_ids)

    # 리스트 전체를 도는 사이트만: 리스트에서 사라진 공고 = 마감. 부분 수집(max_pages)이면 건너뜀
    if src.FULL_LIST and max_pages is None:
        open_before = len(known)
        # 안전장치: 이번에 본 공고가 기존 open 의 절반도 안 되면 API 이상으로 보고 마감 처리 안 함
        if stat["seen"] >= open_before * 0.5:
            conn.execute("CREATE TEMP TABLE seen_now (external_id TEXT PRIMARY KEY)")
            conn.executemany("INSERT INTO seen_now VALUES (?)", [(x,) for x in seen_ids])
            cur = conn.execute(
                """UPDATE postings SET status='closed', closed_at=datetime('now','localtime')
                   WHERE source=? AND status='open'
                     AND external_id NOT IN (SELECT external_id FROM seen_now)""",
                (src.SOURCE,),
            )
            stat["closed"] = cur.rowcount
            conn.execute("DROP TABLE seen_now")
    conn.commit()
    return stat


def _close_expired(conn) -> int:
    """마감일 지난 공고 + 검색형 사이트에서 오래 안 보인 상시 공고."""
    search_sources = [s.SOURCE for s in SOURCES if not s.FULL_LIST]
    cur = conn.execute(
        f"""UPDATE postings SET status='closed', closed_at=datetime('now','localtime')
            WHERE status='open' AND (
                (due_date IS NOT NULL AND due_date < date('now','localtime'))
             OR (due_date IS NULL AND source IN ({','.join('?' * len(search_sources))})
                 AND last_seen_at < datetime('now','localtime', ?)))""",
        (*search_sources, f"-{STALE_DAYS} days"),
    )
    conn.commit()
    return cur.rowcount


_CORP_NOISE = re.compile(r"\(주\)|㈜|주식회사|\(유\)|\(사\)|[^\w]")
_TITLE_NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|【[^】]*】|<[^>]*>")


def _company_key(name: str | None) -> str:
    return _CORP_NOISE.sub("", (name or "").lower())


def _title_key(title: str | None) -> str:
    t = _TITLE_NOISE.sub("", title or "")
    return re.sub(r"[^\w]", "", t.lower())


def _same_title(a: str, b: str) -> bool:
    if not a or not b:
        return False
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return len(short) >= 6 and short in long_


def dedupe(conn) -> int:
    """사이트 간 같은 공고(회사명·제목 정규화 일치) 를 묶는다. 매번 open 공고 전체를 다시 계산.
    같은 사이트 안의 비슷한 공고는 팀이 다를 수 있어 묶지 않는다."""
    rows = conn.execute(
        "SELECT id, source, company, title FROM postings WHERE status='open' AND matched=1"
    ).fetchall()
    rows = sorted(rows, key=lambda r: (_PRIORITY.get(r["source"], 99), r["id"]))
    primaries: dict[str, list] = {}       # company_key → [(id, source, title_key)]
    updates = []
    for r in rows:
        ck, tk = _company_key(r["company"]), _title_key(r["title"])
        dup = None
        for pid, psrc, ptk in primaries.get(ck, []):
            if psrc != r["source"] and _same_title(tk, ptk):
                dup = pid
                break
        if dup is None:
            primaries.setdefault(ck, []).append((r["id"], r["source"], tk))
        updates.append((dup, r["id"]))
    conn.executemany("UPDATE postings SET dup_of=? WHERE id=?", updates)
    conn.commit()
    return sum(1 for d, _ in updates if d is not None)


def collect(*, max_pages: int | None = None, detail_sleep: float = 0.3,
            sources: list | None = None) -> CollectReport:
    rep = CollectReport()
    srcs = sources or SOURCES
    by_name = {s.SOURCE: s for s in SOURCES}

    with get_conn() as conn:
        for n, src in enumerate(srcs):
            batch_status.progress(n, len(srcs))
            try:   # 한 사이트가 막혀도 나머지는 진행
                rep.per_source[src.SOURCE] = _collect_list(conn, src, max_pages)
            except Exception as e:  # noqa: BLE001
                conn.rollback()
                rep.errors.append(f"{src.SOURCE} list: {type(e).__name__}: {e}")
        rep.closed_expired = _close_expired(conn)
        rep.duplicates = dedupe(conn)

        # 상세 — 매칭된 open 대표 공고 중 아직 본문 없는 것만 (중복은 대표 것만 받으면 됨)
        todo = conn.execute(
            f"""SELECT id, source, external_id FROM postings
                WHERE matched=1 AND status='open' AND dup_of IS NULL AND detail_fetched_at IS NULL
                  AND source IN ({','.join('?' * len(srcs))})""",
            [s.SOURCE for s in srcs],
        ).fetchall()
        blocked: set[str] = set()
        for n, row in enumerate(todo):
            batch_status.progress(n, len(todo))
            if row["source"] in blocked:
                continue
            try:
                d = by_name[row["source"]].fetch_detail(row["external_id"])
            except jobkorea.Blocked as e:   # 봇 확인에 걸리면 그 사이트 상세는 다음 회차로
                blocked.add(row["source"])
                rep.errors.append(str(e))
                continue
            except Exception as e:  # noqa: BLE001
                rep.errors.append(f"{row['source']} {row['external_id']}: {e}")
                continue
            conn.execute(
                """UPDATE postings SET intro=?, main_tasks=?, requirements=?, preferred=?, benefits=?,
                       skill_tags=?, body=?, body_images=?, detail_fetched_at=datetime('now','localtime')
                   WHERE id=?""",
                (d["intro"], d["main_tasks"], d["requirements"], d["preferred"], d["benefits"],
                 json.dumps(d["skill_tags"], ensure_ascii=False), d["body"],
                 json.dumps(d["images"], ensure_ascii=False), row["id"]),
            )
            conn.commit()
            rep.details_fetched += 1
            time.sleep(detail_sleep)
    return rep


_FIN = ("employees", "revenue_eok", "op_income_eok", "revenue_growth", "company_type")


def _save_company(conn, source: str, cid: str, c: dict) -> None:
    """재무 컬럼은 새 값이 있을 때만 덮어씀 — 원티드 회사에 사람인에서 보충한 재무가 30일 갱신 때 지워지지 않게."""
    conn.execute(
        f"""INSERT INTO companies (source, external_id, name, founded_year, tags, open_positions, response_rate,
                {', '.join(_FIN)}, fetched_at)
            VALUES (?,?,?,?,?,?,?,{', '.join('?' * len(_FIN))}, datetime('now','localtime'))
            ON CONFLICT (source, external_id) DO UPDATE SET
                name=excluded.name, founded_year=coalesce(excluded.founded_year, companies.founded_year),
                tags=excluded.tags, open_positions=excluded.open_positions, response_rate=excluded.response_rate,
                {', '.join(f'{k}=coalesce(excluded.{k}, companies.{k})' for k in _FIN)},
                fetched_at=excluded.fetched_at""",
        (source, cid, c["name"], c["founded_year"], json.dumps(c["tags"], ensure_ascii=False),
         c["open_positions"], c["response_rate"], *(c[k] for k in _FIN)),
    )


def _name_variants(name: str) -> list[str]:
    """'와드(캐치테이블)' → ['와드', '캐치테이블'] — 괄호 밖 이름과 괄호 안 이름 둘 다 검색어로."""
    outside = re.sub(r"\([^)]*\)", "", name or "").strip()
    inside = [x.strip() for x in re.findall(r"\(([^)]*)\)", name or "")]
    return [v for v in dict.fromkeys([outside, *inside]) if len(_company_key(v)) >= 2]


_SIZE_BUCKET = re.compile(r"([\d,]+)~([\d,]+)명|([\d,]+)명\s*이하|([\d,]+)명\s*이상")


def _size_ok(tags: list[str], employees: int | None) -> bool:
    """원티드 인원 구간 태그와 사람인 사원수가 3배 넘게 어긋나면 동명 다른 회사로 본다."""
    if not employees:
        return True
    for t in tags:
        m = _SIZE_BUCKET.search(t)
        if m:
            n = lambda x: int(x.replace(",", ""))  # noqa: E731
            lo, hi = ((n(m.group(1)), n(m.group(2))) if m.group(1) else (1, n(m.group(3))) if m.group(3)
                      else (n(m.group(4)), 10 ** 7))
            return lo / 3 <= employees <= hi * 3
    return True


def _enrich_from_saramin(conn, cid: str, name: str, founded: int | None, tags: list[str]) -> str | None:
    """원티드 회사의 사원수·매출을 사람인에서 찾아 채운다. 이름이 같은 회사가 여럿이면 설립연도로 고르고,
    원티드 인원 구간과 사원수가 크게 어긋나면 버린다. 확신 못 하면 채우지 않는다. Returns 채택한 csn."""
    keys = {_company_key(v) for v in _name_variants(name)}
    cands: list[str] = []
    for q in _name_variants(name):
        for label, csn in saramin.search_companies(q):
            if _company_key(re.sub(r"\([^)]*\)", "", label)) in keys and csn not in cands:
                cands.append(csn)
        time.sleep(0.3)
    picked = None
    for csn in cands[:4]:
        info = saramin.fetch_company(csn)
        time.sleep(0.3)
        same_year = founded and info["founded_year"] and abs(info["founded_year"] - founded) <= 1
        if not _size_ok(tags, info["employees"]):
            continue
        if same_year or (len(cands) == 1 and not (founded and info["founded_year"])):
            picked = (csn, info)
            break
    if picked:
        csn, info = picked
        conn.execute(
            f"""UPDATE companies SET {', '.join(f'{k}=?' for k in _FIN)}, fin_source=?,
                    fin_checked_at=datetime('now','localtime')
                WHERE source=? AND external_id=?""",
            (*(info[k] for k in _FIN), f"saramin:{csn}", wanted.SOURCE, cid),
        )
    else:
        conn.execute("UPDATE companies SET fin_checked_at=datetime('now','localtime') WHERE source=? AND external_id=?",
                     (wanted.SOURCE, cid))
    conn.commit()
    return picked[0] if picked else None


def refresh_companies(*, max_age_days: int = 30, sleep: float = 0.3) -> tuple[int, list[str]]:
    """매칭된 open 대표 공고의 회사 중 정보가 없거나 오래된 것만 갱신 + 원티드 회사 재무 보충.
    Returns (갱신 수, 오류)."""
    errors: list[str] = []
    done = 0
    by_name = {s.SOURCE: s for s in SOURCES}
    with get_conn() as conn:
        todo = conn.execute(
            """SELECT DISTINCT p.source, p.company_id FROM postings p
               LEFT JOIN companies c ON c.source = p.source AND c.external_id = p.company_id
               WHERE p.matched=1 AND p.status='open' AND p.dup_of IS NULL AND p.company_id IS NOT NULL
                 AND (c.external_id IS NULL OR c.fetched_at < datetime('now','localtime', ?))""",
            (f"-{max_age_days} days",),
        ).fetchall()
        blocked: set[str] = set()
        for n, r in enumerate(todo):
            batch_status.progress(n, len(todo))
            if r["source"] in blocked:
                continue
            mod = by_name[r["source"]]
            try:
                c = mod.fetch_company(r["company_id"])
            except jobkorea.Blocked as e:   # 봇 확인에 걸리면 이번 회차는 그 사이트 중단 → 다음 회차 재시도
                blocked.add(r["source"])
                errors.append(str(e))
                continue
            except Exception as e:  # noqa: BLE001
                errors.append(f"{r['source']} {r['company_id']}: {e}")
                continue
            _save_company(conn, r["source"], r["company_id"], c)
            conn.commit()
            done += 1
            time.sleep(getattr(mod, "COMPANY_SLEEP", sleep))

        # 원티드 회사: 재무 없는 것만 사람인에서 보충 (못 찾은 것도 30일 뒤 재시도)
        enrich = conn.execute(
            """SELECT external_id, name, founded_year, tags FROM companies
               WHERE source=? AND revenue_eok IS NULL AND employees IS NULL
                 AND (fin_checked_at IS NULL OR fin_checked_at < datetime('now','localtime', ?))""",
            (wanted.SOURCE, f"-{max_age_days} days"),
        ).fetchall()
        for n, r in enumerate(enrich):
            batch_status.progress(n, len(enrich))
            try:
                _enrich_from_saramin(conn, r["external_id"], r["name"], r["founded_year"],
                                     json.loads(r["tags"] or "[]"))
            except Exception as e:  # noqa: BLE001
                errors.append(f"enrich {r['name']}: {e}")
    return done, errors
