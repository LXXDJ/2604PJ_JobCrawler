"""네이버 카페 사이트의 메뉴별 게시글 수 표시.

사용:
  python -m scripts.inspect.cafe_menu_counts kotrahochiminh
"""
import sys, io, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from crawlers.fetchers.naver_cafe import crawl_cafe
from crawlers.infra.sites_repo import get_site


def main() -> None:
    if len(sys.argv) < 2:
        print('usage: cafe_menu_counts.py <site_id>')
        sys.exit(1)
    site_id = sys.argv[1]
    s = get_site(site_id)
    if not s:
        print(f'site not found: {site_id}')
        sys.exit(1)
    total = 0
    for src in s['sources']:
        res = crawl_cafe(src['cafe_id'], src['menu_id'])
        n = len(res.rows)
        total += n
        print(f"  {src['menu_name']}: {n}")
    print(f"  총합: {total}")


if __name__ == '__main__':
    main()
