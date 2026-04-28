"""홈 URL 하나 받아서 메뉴 후보 출력 (수동 검증용 CLI)."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.registration.menu_discovery import discover


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("home_url")
    ap.add_argument("--depth1", type=int, default=5,
                    help="홈에서 후보 못 찾을 때 1-depth 탐색할 링크 수")
    args = ap.parse_args()

    result = discover(args.home_url, depth1_top_n=args.depth1)
    out = {
        "home_url": result.home_url,
        "final_home_url": result.final_home_url,
        "ok": result.ok,
        "error": result.error,
        "candidates": [c.to_dict() for c in result.candidates],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
