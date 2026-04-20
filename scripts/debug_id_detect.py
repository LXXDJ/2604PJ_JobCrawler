"""hanin/radiokorea 에서 각 파라미터의 distinct 숫자값 개수 디버그."""
import sys, io, re
from urllib.parse import parse_qs, urlparse
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.path.insert(0, "crawlers")
from http_client import fetch

for label, url in [
    ("hanin", "http://www.hanin.or.kr/bbs/board.php?bo_table=Information"),
    ("radiokorea", "https://www.radiokorea.com/community/jobs.php"),
]:
    html = fetch(url, timeout=30)
    hrefs = re.findall(r'href="([^"]+)"', html)
    # 먼저 wr_id 들어간 href 샘플 직접 출력 (hanin 디버그)
    if label == "hanin":
        wr_hrefs = [h for h in hrefs if "wr_id" in h]
        print(f"[hanin] total hrefs={len(hrefs)}, hrefs with wr_id={len(wr_hrefs)}")
        for h in wr_hrefs[:5]:
            print(f"  {h}")
        print()
    param_values = {}
    for h in hrefs:
        try:
            qs = parse_qs(urlparse(h).query)
        except Exception:
            continue
        for k, vs in qs.items():
            for v in vs:
                if v.isdigit() and len(v) >= 2:
                    param_values.setdefault(k, set()).add(v)
    print(f"=== {label} (digit_len>=2) ===")
    for k in sorted(param_values, key=lambda x: -len(param_values[x])):
        if len(param_values[k]) < 3:
            continue
        print(f"  {k:15} distinct={len(param_values[k]):3} sample={list(param_values[k])[:3]}")
    print()
