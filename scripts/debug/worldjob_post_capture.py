"""worldjob 사이트가 실제로 /advnc/ajax/getEpmtList.do 호출 시 보내는 POST body 캡처."""
import io
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from playwright.sync_api import sync_playwright


URL = "https://www.worldjob.or.kr/advnc/epmtList.do?menuId=1000002033"


def main():
    captured = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        page = ctx.new_page()

        def on_request(req):
            if "getEpmtList" in req.url:
                captured.append({
                    "url": req.url,
                    "method": req.method,
                    "post_data": req.post_data,
                    "headers": dict(req.headers),
                })

        page.on("request", on_request)
        page.goto(URL, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        page.wait_for_timeout(2000)
        browser.close()

    print(f"captured {len(captured)} requests to getEpmtList:")
    for i, c in enumerate(captured):
        print(f"\n[{i}] {c['method']} {c['url']}")
        print(f"    post_data: {c['post_data']!r}")
        print(f"    headers (subset):")
        for k in ("content-type", "x-requested-with", "referer", "cookie"):
            v = c["headers"].get(k) or c["headers"].get(k.title())
            if v:
                print(f"      {k}: {v[:120]}")


if __name__ == "__main__":
    main()
