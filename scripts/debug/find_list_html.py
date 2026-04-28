"""HTML 안에서 채용 list 같아 보이는 패턴 탐색.

힌트:
  - URL 에 'View'/'Detail' 키워드 + 숫자 query/path
  - script 태그 안의 JSON / form action url
"""
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.fetchers.static import fetch as fetch_static
from crawlers.fetchers.dynamic import fetch as fetch_dynamic


def main(url: str, mode: str = "static"):
    r = fetch_dynamic(url, capture_api=True) if mode == "dynamic" else fetch_static(url)
    html = r.text or ""
    print(f"len={len(html)}")

    # 1. detail-style URLs in HTML
    print("\n--- view/detail URLs ---")
    pat = re.compile(r"['\"]([^'\"]*?(?:View|view|Detail|detail|Read)\.do[^'\"]*?)['\"]")
    seen = set()
    for m in pat.finditer(html):
        u = m.group(1)
        if u not in seen and len(seen) < 20:
            seen.add(u)
            print(" ", u[:120])

    # 2. recruitSeq 같은 ID query 가 박힌 anchor
    print("\n--- ID query patterns ---")
    pat2 = re.compile(r"(?:recruitSeq|articleNo|bbscttNo|seq|postNo|jobSeq|recrtNo|recrt)=\d+", re.I)
    s = set(pat2.findall(html))
    for x in list(s)[:10]:
        print(" ", x)

    # 3. form action
    print("\n--- form actions ---")
    pat3 = re.compile(r'<form[^>]+action="([^"]+)"', re.I)
    for m in pat3.finditer(html):
        print(" ", m.group(1)[:120])

    # 4. ajax-like XHR call setup in scripts
    print("\n--- XHR setup hints ---")
    pat4 = re.compile(r'(?:url\s*[:=]\s*[\'"])(/[^\'"]+\.do[^\'"]*)[\'"]', re.I)
    s4 = set(m.group(1) for m in pat4.finditer(html))
    for x in list(s4)[:20]:
        print(" ", x[:120])

    if mode == "dynamic":
        api = getattr(r, "api_calls", None) or []
        print(f"\n--- dynamic API calls: {len(api)}")
        for c in api[:10]:
            print(f"  [{c.get('status')}] {c.get('url')[:120]}")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else "static")
