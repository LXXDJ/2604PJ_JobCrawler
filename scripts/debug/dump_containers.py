"""URL 의 list 후보 컨테이너 전체 dump."""
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.fetchers.static import fetch as fetch_static
from crawlers.fetchers.dynamic import fetch as fetch_dynamic
from crawlers.extractors.list_extractor import extract_list_multi


def main(url: str, mode: str = "static"):
    r = fetch_dynamic(url) if mode == "dynamic" else fetch_static(url)
    print(f"ok={r.ok} len={len(r.text)}")
    multi = extract_list_multi(r.text, r.final_url)
    print(f"\ncontainers: {len(multi.candidates)}")
    for i, ext in enumerate(multi.candidates):
        # detail URL paths
        from urllib.parse import urlparse
        paths = [urlparse(row.detail_url).path for row in ext.rows]
        path_set = sorted(set(paths))
        # title sample
        titles = [(row.title or "")[:25] for row in ext.rows[:3]]
        print(f"\n[{i:2}] {ext.container_signature}  rows={ext.count}")
        print(f"     paths ({len(path_set)} unique): {path_set[:3]}")
        print(f"     titles: {titles}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "static")
