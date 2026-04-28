"""사이트 탐색 — sitemap / robots / 통상 path 시도."""
import io
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from crawlers.fetchers.static import fetch as fetch_static


def main(base: str):
    print(f"base: {base}\n")

    # robots.txt
    print("--- /robots.txt ---")
    r = fetch_static(urljoin(base, "/robots.txt"))
    if r.ok:
        print(r.text[:1500])
    else:
        print(f"fail: {r.error or r.status}")

    # sitemap.xml
    print("\n--- /sitemap.xml ---")
    r = fetch_static(urljoin(base, "/sitemap.xml"))
    if r.ok:
        # urls 추출
        urls = re.findall(r"<loc>([^<]+)</loc>", r.text)
        print(f"  ok len={len(r.text)} urls={len(urls)}")
        for u in urls[:25]:
            print(f"    {u}")
    else:
        print(f"  fail: {r.error or r.status}")

    # 통상적 list paths
    common_paths = [
        "/jobs.html", "/job.html", "/jobs", "/job",
        "/position.htm", "/positions",
        "/company/joblist.htm", "/joblist.htm",
        "/index/joblist.htm", "/job/list",
        "/job_search.htm", "/search.htm",
        "/api/jobs", "/api/joblist",
    ]
    print("\n--- 통상 path 시도 ---")
    for p in common_paths:
        u = urljoin(base, p)
        r = fetch_static(u)
        size = len(r.text) if r.ok else 0
        print(f"  [{r.status:3}] {u}  len={size}")


if __name__ == "__main__":
    main(sys.argv[1])
