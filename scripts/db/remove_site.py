"""사이트 1개 + 그 사이트의 jobs / crawl_runs 모두 삭제 (FK CASCADE).

사용:
  python -m scripts.remove_site <site_id>
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.infra.db import get_conn


def remove(site_id: str) -> None:
    with get_conn() as c:
        s = c.execute("SELECT id FROM sites WHERE id = ?", (site_id,)).fetchone()
        if not s:
            print(f"site not found: {site_id}")
            return
        n_jobs = c.execute("SELECT COUNT(*) n FROM jobs WHERE site_id = ?", (site_id,)).fetchone()["n"]
        n_runs = c.execute("SELECT COUNT(*) n FROM crawl_runs WHERE site_id = ?", (site_id,)).fetchone()["n"]
        c.execute("DELETE FROM sites WHERE id = ?", (site_id,))
        print(f"removed site={site_id} (cascaded jobs={n_jobs}, runs={n_runs})")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        sys.exit(1)
    remove(sys.argv[1])
