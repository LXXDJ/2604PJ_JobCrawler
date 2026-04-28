"""worldjob AJAX 에 실제 form body 로 POST 시 결과 확인."""
import io
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

try:
    from curl_cffi import requests as cffi
except ImportError:
    cffi = None

URL = "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do"
HEADERS = {
    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.worldjob.or.kr/advnc/epmtList.do?menuId=1000002033",
}
GOVIEW_PAT = re.compile(r"goView1\('([^']+)'")


def post(page: int, page_size: int = 50):
    body = (
        f"showItemListCount={page_size}&listUrl=%2Fadvnc%2FcnttList.do"
        f"&joCrtfcNo=&joCrtfcDsp=&joCrtfcDspSn=&pageIndex={page}"
        f"&orderByType=DESC&orderByKey=A.DTA_RGST_DT"
        f"&itrnBsnsClsnNo=&opertnInsttCd=&rctntcNo=&tabCheck="
        f"&dobType=1&viewVersion=V2"
        f"&keyword=&hKeyword=&hJoCrtfcNo="
    )
    r = cffi.post(URL, data=body, headers=HEADERS, impersonate="chrome124", timeout=20)
    return r.text


def main():
    all_ids: set[str] = set()
    for page in range(1, 14):
        html = post(page, page_size=50)
        ids = list(dict.fromkeys(GOVIEW_PAT.findall(html)))
        unique = set(ids)
        new = len(unique - all_ids)
        all_ids.update(unique)
        boxes = html.count('class="post-box"')
        print(f"page {page:2}  boxes={boxes:3}  raw_anchors={len(ids):3}  unique={len(unique):3}  new_global={new:3}")
        if not ids:
            break
    print(f"\n총 unique IDs: {len(all_ids)}")


if __name__ == "__main__":
    main()
