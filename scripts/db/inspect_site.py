"""sites + jobs 일부 출력. 사용: python -m scripts.inspect_site <site_id>"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.infra.db import get_conn


def main(site_id: str) -> None:
    with get_conn() as c:
        s = c.execute(
            "SELECT id, home_url, name, status, sources FROM sites WHERE id=?",
            (site_id,),
        ).fetchone()
        if not s:
            print(f"site not found: {site_id}")
            return
        d = dict(s)
        d["sources"] = json.loads(d["sources"])
        print("=== site ===")
        print(json.dumps(d, ensure_ascii=False, indent=2))
        print()
        print("=== jobs (10) ===")
        for r in c.execute(
            "SELECT external_id, title, url FROM jobs WHERE site_id=? LIMIT 10",
            (site_id,),
        ):
            print(f" - {r['external_id']:20} | {(r['title'] or '')[:60]:60} | {r['url'][:90]}")


if __name__ == "__main__":
    main(sys.argv[1])
