"""worldjob 12 페이지 전체 라이브 검증 — 어디서 row 누락되는지 정확히 추적.

각 페이지에서:
- raw post-box 수
- raw goView1 anchor unique ID 수
- 우리 list_extractor 추출 결과
- 페이지 간 ID 중복 여부
"""
import io
import re
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from bs4 import BeautifulSoup
from crawlers.fetchers.static import fetch as fetch_static
from crawlers.extractors.list_extractor import extract_list_multi


BASE = "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do"
HEADERS = {
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.worldjob.or.kr/advnc/epmtList.do",
}
GOVIEW_PAT = re.compile(r"goView1\('([^']+)'")


def main():
    all_raw_ids: set[str] = set()       # 모든 페이지 raw goView1 ID 합집합
    all_extracted_urls: set[str] = set()  # 우리 extractor 채택 URL 합집합
    per_page = []

    for page in range(1, 14):
        url = f"{BASE}?pageIndex={page}"
        r = fetch_static(url, headers=HEADERS)
        if not r.ok:
            print(f"page {page}: fail")
            break

        # raw post-box / goView1 ID
        s = BeautifulSoup(r.text, "html.parser")
        boxes = s.select("div.post-box")
        raw_ids: list[str] = []
        for box in boxes:
            for a in box.find_all("a", href=True):
                m = GOVIEW_PAT.search(a["href"])
                if m:
                    raw_ids.append(m.group(1))
                    break
        unique_raw_ids = set(raw_ids)

        # 우리 추출
        multi = extract_list_multi(r.text, url)
        ext_urls = set()
        for ext in multi.candidates:
            for row in ext.rows:
                ext_urls.add(row.detail_url)
        # 단순화: 같은 컨테이너 (post-box) 만
        post_box_ext = next(
            (e for e in multi.candidates if "post-box" in e.container_signature),
            None,
        )
        post_box_urls = set(row.detail_url for row in (post_box_ext.rows if post_box_ext else []))

        new_raw = len(unique_raw_ids - all_raw_ids)
        new_ext = len(post_box_urls - all_extracted_urls)
        all_raw_ids.update(unique_raw_ids)
        all_extracted_urls.update(post_box_urls)

        per_page.append({
            "page": page, "boxes": len(boxes),
            "raw_ids": len(raw_ids),
            "unique_raw_ids": len(unique_raw_ids),
            "post_box_extracted": len(post_box_urls),
            "new_raw_in_global": new_raw,
            "new_extracted_in_global": new_ext,
        })
        print(f"page {page:2}  boxes={len(boxes):2}  raw_ids={len(raw_ids):2}"
              f"  unique={len(unique_raw_ids):2}  extracted={len(post_box_urls):2}"
              f"  new_raw_global={new_raw:2}  new_ext_global={new_ext:2}")

        # raw 와 extracted 차이가 있으면 누락 ID 표시
        if len(unique_raw_ids) > len(post_box_urls):
            # post_box_urls 에서 _jsid 추출
            ext_ids = set()
            for u in post_box_urls:
                m = re.search(r"_jsid=([^&]+)", u)
                if m:
                    ext_ids.add(m.group(1))
            missing = unique_raw_ids - ext_ids
            if missing:
                print(f"          ⚠ 페이지 안에서 누락된 raw ID: {sorted(missing)[:5]}...")

    print(f"\n=== 합계 ===")
    print(f"raw goView1 unique IDs (12 페이지 합집합): {len(all_raw_ids)}")
    print(f"우리 extracted unique URLs:              {len(all_extracted_urls)}")
    print(f"누락: {len(all_raw_ids) - len(all_extracted_urls)}건")

    # raw 중에 우리가 빠뜨린 ID
    ext_ids_global = set()
    for u in all_extracted_urls:
        m = re.search(r"_jsid=([^&]+)", u)
        if m:
            ext_ids_global.add(m.group(1))
    missing_global = all_raw_ids - ext_ids_global
    print(f"누락 ID 샘플: {sorted(missing_global)[:10]}")


if __name__ == "__main__":
    main()
