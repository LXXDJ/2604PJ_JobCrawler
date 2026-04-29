"""홈 URL 하나/여러 개 등록 CLI.

사용:
  python scripts/register.py https://www.career.co.kr
  python scripts/register.py --dry-run https://example.com
  python scripts/register.py --file urls.txt
"""
from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from crawlers.infra.db import init_db
from crawlers.registration.naver_cafe import is_naver_cafe_url, register_naver_cafe
from crawlers.registration.register import register


def _print_cafe_report(rep) -> None:
    print(f"\n[{rep.final_status.upper()}] {rep.home_url}  (site_id={rep.site_id})  [naver_cafe]")
    if rep.cafe_id:
        print(f"  cafe: id={rep.cafe_id} slug={rep.cafe_slug} name={rep.cafe_name}")
    if rep.candidate_menus:
        print(f"  job menus matched: {len(rep.candidate_menus)}")
        for m in rep.candidate_menus:
            print(f"    - {m['menuId']:>4}  {m['name']}")
    if rep.sources:
        print(f"  sources ({len(rep.sources)}):")
        for s in rep.sources:
            print(f"    - menu={s['menu_id']} ({s.get('menu_name','')})  "
                  f"rows={s['list_rows']}")
    for note in rep.notes:
        print(f"  note: {note}")


def _print_report(rep) -> None:
    print(f"\n[{rep.final_status.upper()}] {rep.home_url}  (site_id={rep.site_id})")
    if rep.discovery:
        print(f"  discovery: {len(rep.discovery.candidates)} candidates")
    if rep.classifications:
        by = {"full": 0, "filtered": 0, "personal": 0, "unknown": 0}
        for c in rep.classifications:
            by[c.label] = by.get(c.label, 0) + 1
        print(f"  classify: full={by['full']} filtered={by['filtered']} "
              f"personal={by['personal']} unknown={by['unknown']}")
    if rep.validations:
        ok = sum(1 for v in rep.validations if v.ok)
        print(f"  validate: {ok}/{len(rep.validations)} passed")
    if rep.dedupe:
        print(f"  dedupe: kept={len(rep.dedupe.kept)} dropped={len(rep.dedupe.dropped)}")
    if rep.sources:
        print(f"  sources ({len(rep.sources)}):")
        for s in rep.sources:
            print(f"    - [{s['label']}] {s['url']}  rows={s['list_rows']} "
                  f"link_ratio={s['subject_link_ratio']}")
    for note in rep.notes:
        print(f"  note: {note}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--file", help="URL list file (1 per line)")
    ap.add_argument("--top", type=int, default=300,
                    help="LLM 분류할 최대 후보 수 (기본 300=사실상 무제한)")
    ap.add_argument("--snippet", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--name", default=None)
    args = ap.parse_args()

    urls: list[str] = list(args.urls)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            urls += [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]

    if not urls:
        ap.error("URL을 인자나 --file 로 전달하세요")

    if not args.dry_run:
        init_db()

    for url in urls:
        if is_naver_cafe_url(url):
            rep = register_naver_cafe(url, name=args.name, dry_run=args.dry_run)
            _print_cafe_report(rep)
        else:
            rep = register(
                url,
                name=args.name,
                top_n=args.top,
                use_snippet=args.snippet,
                dry_run=args.dry_run,
            )
            _print_report(rep)


if __name__ == "__main__":
    main()
