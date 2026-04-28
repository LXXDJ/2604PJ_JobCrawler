"""worldjob page 1 의 anchor 들을 모두 보고 우리가 왜 47/50 만 잡는지 확인.

list_extractor 의 _extract_subject 는 row 내부에서 '가장 긴 anchor' 를 title 로 채택.
실제 list_extractor 결과와 raw post-box 갯수 비교.
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


URL = "https://www.worldjob.or.kr/advnc/ajax/getEpmtList.do"

r = fetch_static(URL, headers={
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://www.worldjob.or.kr/advnc/epmtList.do",
})
s = BeautifulSoup(r.text, "html.parser")

# 모든 post-box 안의 anchor 분석
boxes = s.select("div.post-box")
print(f"div.post-box 갯수: {len(boxes)}")

from crawlers.extractors.list_extractor import extract_list_multi, _extract_subject

# 1. extract_list_multi 결과 확인
multi = extract_list_multi(r.text, URL)
post_box_ext = next((e for e in multi.candidates
                     if "post-box" in e.container_signature), None)
print(f"extract_list_multi → post-box container rows: {post_box_ext.count if post_box_ext else 0}")

# 2. raw box 별로 _extract_subject 결과 — 누가 빠지나?
extracted = []
for i, box in enumerate(boxes):
    sub = _extract_subject(box, URL)
    if sub is None:
        extracted.append((i, None, None, None))
    else:
        # box 안의 모든 anchor 도 같이 보기
        anchors = [(a.get_text(" ", strip=True), a["href"][:60])
                   for a in box.find_all("a", href=True)]
        extracted.append((i, sub.title[:40], sub.detail_url, anchors))

# 3. _extract_subject 가 None 인 box / goView1 이 아닌 anchor 를 잡은 box 찾기
print(f"\n_extract_subject = None 인 box:")
for i, t, u, anchors in extracted:
    if t is None:
        print(f"  #{i}  anchors: {anchors[:3]}")

print(f"\n_extract_subject 가 goView1 이 아닌 anchor 를 채택한 box:")
n_wrong = 0
for i, t, u, anchors in extracted:
    if u and "_jsid" not in u:
        n_wrong += 1
        if n_wrong <= 5:
            print(f"  #{i}  title={t!r}  url={u[:80]}")
            print(f"      box's anchors: {anchors[:5]}")
print(f"  ... total wrong: {n_wrong}")

# unique detail_url 갯수
all_urls = [u for i, t, u, _ in extracted if u]
unique_urls = set(all_urls)
print(f"\n총 detail_url: {len(all_urls)}")
print(f"unique detail_url: {len(unique_urls)}")
if len(unique_urls) < len(all_urls):
    from collections import Counter
    dup = [u for u, n in Counter(all_urls).items() if n > 1]
    print(f"중복 URL ({len(dup)}개):")
    for u in dup[:5]:
        # 어떤 box 들이 같은 URL?
        idxs = [i for i, t, ux, _ in extracted if ux == u]
        print(f"  {u[:90]}  (box {idxs})")

# 사용자가 직접 검증할 수 있게 box 45, 47 의 모든 anchor + 회사명/직종 text 출력
print("\n=== box 45, 47 raw HTML / anchor / 회사명 / 직종 ===")
for idx in [45, 47]:
    box = boxes[idx]
    print(f"\n--- box #{idx}")
    title_text = box.get_text(" ", strip=True)[:200]
    print(f"  텍스트: {title_text}")
    print("  anchor 들:")
    for a in box.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(" ", strip=True)
        print(f"    href={href}")
        print(f"      text={text!r}")
