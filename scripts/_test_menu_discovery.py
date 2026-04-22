"""menu_discovery 동작 확인 — 사이트 주면 구인 메뉴 후보 list 출력."""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "crawlers"))

from menu_discovery import discover_job_menus

target = sys.argv[1] if len(sys.argv) > 1 else "https://www.albamon.com/"
print(f'\n=== 메뉴 탐색: {target} ===\n')
menus = discover_job_menus(target, timeout_ms=20000)
print(f'발견 {len(menus)}개 후보\n')
for m in menus:
    print(f"  [{m['score']:3d}] {m['menu_name']:40s}  {m['url']}")
