"""DB 초기화 CLI."""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.infra.db import DB_PATH, init_db


def main() -> None:
    init_db()
    print(f"OK: DB initialized at {DB_PATH}")


if __name__ == "__main__":
    main()
