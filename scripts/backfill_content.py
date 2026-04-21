"""content 비어있는 기존 공고의 detail 페이지를 재수집해 채워 넣는다.

dom_crawler 는 신규 공고에 한해서만 detail fetch 하기 때문에, 과거 버그
(예: ppomppu 상대경로 링크 urljoin 누락) 로 content 가 빈 채 insert 된
행들은 재수집해도 갱신되지 않는다. 이 스크립트가 그 backfill 경로.

사용:
    python scripts/backfill_content.py <site_id> [--limit N] [--dry-run]
"""
import argparse
import json
import os
import sqlite3
import sys
import time
from io import TextIOWrapper
from urllib.parse import urlparse, urljoin

sys.stdout = TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "crawlers"))

from http_client import fetch
from dom_crawler import _parse_detail_page

DB_PATH = os.path.join(ROOT, "data", "jobs.db")
SITES_PATH = os.path.join(ROOT, "data", "sites.json")


def _resolve_link(link: str, source: dict) -> str:
    """상대경로 링크를 list_url 기준으로 절대화."""
    if not link:
        return ""
    if urlparse(link).scheme:
        return link
    base = source.get("list_url") or source.get("base_url", "")
    return urljoin(base, link) if base else link


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("site_id")
    ap.add_argument("--limit", type=int, default=None, help="처리할 최대 행 수 (테스트용)")
    ap.add_argument("--dry-run", action="store_true", help="DB update 생략")
    ap.add_argument("--sleep", type=float, default=0.5, help="요청 간 딜레이 (초)")
    args = ap.parse_args()

    with open(SITES_PATH, encoding="utf-8") as f:
        entries = json.load(f)
    entry = next((e for e in entries if e["site_id"] == args.site_id), None)
    if not entry:
        print(f"[ERR] site_id '{args.site_id}' not in sites.json")
        sys.exit(1)

    source = entry.get("source") or {}
    content_sel = (source.get("selectors") or {}).get("content")
    if not content_sel:
        print(f"[ERR] {args.site_id} has no selectors.content — nothing to backfill")
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row

    where = "source = ? AND (content = '' OR content IS NULL) AND link != ''"
    q = f"SELECT id, external_id, link FROM jobs WHERE {where} ORDER BY id"
    if args.limit:
        q += f" LIMIT {args.limit}"
    rows = con.execute(q, (args.site_id,)).fetchall()

    print(f"[{args.site_id}] backfill 대상: {len(rows)}건  content_sel={content_sel!r}  dry_run={args.dry_run}")
    if not rows:
        return

    http_kwargs = {"timeout": 30, "max_retries": 2, "retry_backoff": 1.5}
    ok = 0
    empty = 0
    fail = 0
    for i, r in enumerate(rows, 1):
        url = _resolve_link(r["link"], source)
        try:
            html = fetch(url, **http_kwargs)
            content = _parse_detail_page(html, entry, url, http_kwargs)
        except Exception as e:
            fail += 1
            print(f"  [FAIL {i}/{len(rows)}] id={r['id']} ext={r['external_id']} — {type(e).__name__}: {str(e)[:80]}")
            time.sleep(args.sleep)
            continue

        if not content:
            empty += 1
            if empty <= 3:
                print(f"  [EMPTY {i}/{len(rows)}] id={r['id']} ext={r['external_id']} selector 매치 없음")
        else:
            ok += 1

        if not args.dry_run:
            con.execute(
                "UPDATE jobs SET content = ?, link = ? WHERE id = ?",
                (content, url, r["id"]),
            )
            con.commit()

        if i % 50 == 0:
            print(f"  진행 {i}/{len(rows)} — ok={ok} empty={empty} fail={fail}")
        time.sleep(args.sleep)

    print(f"\n[{args.site_id}] 완료: ok={ok} empty={empty} fail={fail}  총 {len(rows)}건")


if __name__ == "__main__":
    main()
