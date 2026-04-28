"""dry-run 으로 register 하면서 각 candidate 의 분류/검증 결과 상세 출력."""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.registration.register import register


def main(url: str):
    rep = register(url, dry_run=True)
    print(f"\n[{rep.final_status}] {url}  site_id={rep.site_id}")
    print(f"discovery: {len(rep.discovery.candidates) if rep.discovery else 0}")
    print(f"\n--- classifications (full + filtered) ---")
    for c in rep.classifications:
        if c.label in ("full", "filtered"):
            print(f"  [{c.label:8}] score={getattr(c, 'score', '-')} {c.url}")
            print(f"    reason: {(c.reason or '')[:120]}")

    print(f"\n--- validations ({len(rep.validations)}) ---")
    for v in rep.validations:
        ok = "OK  " if v.ok else "FAIL"
        print(f"  {ok} rows={v.list_rows:3} subj={v.subject_link_ratio:.2f} title={v.job_title_ratio:.2f} fetcher={v.fetcher} {v.url}")
        if not v.ok:
            print(f"    reason: {v.reason or v.error}")
        if v.sample_titles:
            print(f"    titles: {v.sample_titles[:3]}")
    for n in rep.notes:
        print(f"  note: {n}")


if __name__ == "__main__":
    main(sys.argv[1])
