"""특정 site_id 의 jobs 삭제 (사이트 자체는 유지)."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.infra.db import get_conn

with get_conn() as c:
    n = c.execute("DELETE FROM jobs WHERE site_id=?", (sys.argv[1],)).rowcount
    print(f"deleted {n} jobs for site_id={sys.argv[1]}")
