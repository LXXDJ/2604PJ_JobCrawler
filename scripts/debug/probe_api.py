"""Playwright capture_api 결과의 JSON XHR endpoints 보기."""
import json
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.fetchers.dynamic import fetch as fetch_dynamic


def main(url: str):
    r = fetch_dynamic(url, capture_api=True)
    print(f"ok={r.ok} status={r.status} final={r.final_url}")
    api_calls = getattr(r, "api_calls", None) or []
    print(f"API calls captured: {len(api_calls)}")
    for c in api_calls:
        data = c.get("data")
        kind = type(data).__name__
        size = ""
        if isinstance(data, dict):
            size = f"keys={len(data)}: {list(data)[:5]}"
        elif isinstance(data, list):
            size = f"items={len(data)}"
        print(f"  [{c.get('status')}] {c.get('url')}\n    type={kind} {size}")


if __name__ == "__main__":
    main(sys.argv[1])
