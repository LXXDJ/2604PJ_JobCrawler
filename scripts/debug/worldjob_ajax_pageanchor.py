"""worldjob AJAX 응답에 페이지 navigation anchor 가 있는지 확인."""
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.fetchers.static import fetch as fetch_static


URL = "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do"
HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.worldjob.or.kr/advnc/epmtList.do",
}


def main():
    r = fetch_static(URL, headers=HEADERS)
    html = r.text or ""
    print(f"len={len(html)}")

    patterns = [
        ("javascript:fn page calls", r"javascript:[A-Za-z_][\w$]*[Pp]age[^'\"]*"),
        ("pageIndex=N anchors",      r"pageIndex=\d+"),
        ("currentPage=N",            r"currentPage=\d+"),
        ("paging div / class",       r'class="[^"]*pag[^"]*"'),
        ("totalCount / total / pages", r"(total[^\s<>]+|totalCount[^\s<>]+|pages?[^\s<>]*)\s*[:=]\s*['\"]?\d+"),
    ]
    for name, pat in patterns:
        ms = re.findall(pat, html)[:8]
        print(f"\n  {name}")
        for m in ms:
            print(f"    {m[:100]}")


if __name__ == "__main__":
    main()
