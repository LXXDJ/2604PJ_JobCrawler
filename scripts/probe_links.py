"""ppomppu/radiokorea 상세링크 패턴 확인."""
import sys, io, re
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, ".")
sys.path.insert(0, "crawlers")
from http_client import fetch

for url in [
    "https://www.ppomppu.co.kr/zboard/zboard.php?id=guin",
    "https://www.radiokorea.com/community/jobs.php",
]:
    print(f"=== {url} ===")
    try:
        html = fetch(url, timeout=30)
    except Exception as e:
        print(f"  fail: {e}")
        continue
    links = re.findall(r'href="([^"]+)"', html)
    seen = set()
    for l in links:
        if len(l) > 200: continue
        # 의심스러운 상세링크: view/read/mode=view/wr_id/no=
        if re.search(r"view_table|mode=view|wr_id=|no=\d|view\.php|read\.php|\?id=.*&no=", l):
            if l in seen: continue
            seen.add(l)
            print(f"  {l}")
            if len(seen) >= 10: break
    print()
