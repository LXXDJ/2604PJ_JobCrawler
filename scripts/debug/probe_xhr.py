"""특정 endpoint 를 직접 호출해서 응답 일부 보기."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from crawlers.fetchers.static import fetch as fetch_static


def main(url: str):
    r = fetch_static(url, headers={
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "*/*",
        "Referer": "https://www.worldjob.or.kr/advnc/epmtList.do",
    })
    print(f"ok={r.ok} status={r.status} len={len(r.text)}")
    print(r.text[:2000])


if __name__ == "__main__":
    main(sys.argv[1])
