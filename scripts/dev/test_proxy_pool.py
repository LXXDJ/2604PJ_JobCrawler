"""proxy_pool 동작 검증 — 각 proxy 로 ip 확인 사이트 호출 + hibrain 빠른 GET."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawlers"))

from crawlers.proxy_pool import GLOBAL
from curl_cffi import requests as cr

print(f"풀 로드: {len(GLOBAL)}개 proxy")
if not GLOBAL:
    sys.exit("proxies.json 비어있음")

# 1) 각 proxy 로 외부 IP 확인
print("\n[1] 각 proxy 로 ip 확인 (api.ipify.org)")
for p in GLOBAL._proxies:
    url = GLOBAL.to_url(p)
    try:
        r = cr.get("https://api.ipify.org?format=json",
                   proxies={"http": url, "https": url},
                   impersonate="chrome131", timeout=15, verify=False)
        print(f"  {p['host']:>16}:{p['port']} → {r.json()}")
    except Exception as e:
        print(f"  {p['host']:>16}:{p['port']} → FAIL {type(e).__name__}: {str(e)[:80]}")

# 2) hibrain 첫 요청 — proxy 통한 200 받는지
print("\n[2] hibrain 첫 GET (proxy 경유)")
for p in GLOBAL._proxies[:3]:
    url = GLOBAL.to_url(p)
    try:
        r = cr.get("https://www.hibrain.net/recruitment/categories/JOB/categories/RES/recruits",
                   proxies={"http": url, "https": url},
                   impersonate="chrome131", timeout=20, verify=False)
        print(f"  {p['host']:>16}:{p['port']} → status={r.status_code} body={len(r.text)}자")
    except Exception as e:
        print(f"  {p['host']:>16}:{p['port']} → FAIL {type(e).__name__}: {str(e)[:80]}")
