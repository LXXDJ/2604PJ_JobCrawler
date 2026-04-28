"""기존 site_id 를 새 site_id 로 rename. sites + jobs + crawl_runs 모두 이전.

사용:
  python scripts/rename_site_id.py <old_id> <new_id>
"""
from __future__ import annotations

import sys

from crawlers.infra.db import get_conn


def rename(old_id: str, new_id: str) -> None:
    with get_conn() as c:
        old = c.execute("SELECT id FROM sites WHERE id = ?", (old_id,)).fetchone()
        if not old:
            print(f"old site not found: {old_id}")
            return
        clash = c.execute("SELECT id FROM sites WHERE id = ?", (new_id,)).fetchone()
        if clash:
            print(f"new site_id already exists: {new_id} — abort")
            return

        # FK 가 ON UPDATE CASCADE 가 아니므로 잠시 끄고 일괄 update
        c.execute("PRAGMA foreign_keys = OFF")
        try:
            c.execute("UPDATE sites SET id = ? WHERE id = ?", (new_id, old_id))
            c.execute("UPDATE jobs SET site_id = ? WHERE site_id = ?", (new_id, old_id))
            c.execute("UPDATE crawl_runs SET site_id = ? WHERE site_id = ?", (new_id, old_id))
        finally:
            c.execute("PRAGMA foreign_keys = ON")

        n_jobs = c.execute("SELECT COUNT(*) n FROM jobs WHERE site_id = ?", (new_id,)).fetchone()["n"]
        n_runs = c.execute("SELECT COUNT(*) n FROM crawl_runs WHERE site_id = ?", (new_id,)).fetchone()["n"]
        print(f"renamed {old_id} -> {new_id} (jobs={n_jobs}, runs={n_runs})")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(__doc__)
        sys.exit(1)
    rename(sys.argv[1], sys.argv[2])
