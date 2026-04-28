"""sitemap 후보 필터 테스트."""
import io
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from crawlers.registration.menu_discovery import _fetch_sitemap_links

links = _fetch_sitemap_links(sys.argv[1])
print(f"sitemap candidates after filter: {len(links)}")
for u, _ in links[:50]:
    print(f"  {u}")
