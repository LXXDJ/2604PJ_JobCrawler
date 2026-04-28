"""홈 fetch 결과 디버그용. 정적 fetch 후 <a> 링크 + 텍스트 일부 출력."""
import io
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from bs4 import BeautifulSoup
from crawlers.fetchers.static import fetch as fetch_static
from crawlers.fetchers.dynamic import fetch as fetch_dynamic


def main(url: str) -> None:
    print(f"=== STATIC: {url}")
    r = fetch_static(url)
    print(f"  ok={r.ok} status={r.status} final={r.final_url} len={len(r.text or '')}")
    if r.ok:
        s = BeautifulSoup(r.text, "html.parser")
        anchors = s.find_all("a", href=True)
        print(f"  <a href>: {len(anchors)}")
        for a in anchors[:30]:
            print(f"    href={a['href'][:80]!r:80}  text={a.get_text(' ', strip=True)[:40]!r}")

    print(f"\n=== DYNAMIC: {url}")
    rd = fetch_dynamic(url)
    print(f"  ok={rd.ok} status={rd.status} final={rd.final_url} len={len(rd.text or '')}")
    if rd.ok:
        s = BeautifulSoup(rd.text, "html.parser")
        anchors = s.find_all("a", href=True)
        print(f"  <a href>: {len(anchors)}")
        for a in anchors[:30]:
            print(f"    href={a['href'][:80]!r:80}  text={a.get_text(' ', strip=True)[:40]!r}")


if __name__ == "__main__":
    main(sys.argv[1])
