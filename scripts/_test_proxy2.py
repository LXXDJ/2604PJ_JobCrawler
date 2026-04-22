"""작은 응답 내용 검사 — CF challenge 인지, 실제 HTML 인지 구분."""
import sys
import requests

sys.stdout.reconfigure(encoding='utf-8')

PROXY = "http://lscbspwf:5vvmixnb5mw9@31.59.20.176:6754"
proxies = {"http": PROXY, "https": PROXY}
ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}

for label, url in [
    ("리멤버", "https://career.rememberapp.co.kr/"),
    ("헤이코리안", "https://www.heykorean.com/"),
]:
    r = requests.get(url, proxies=proxies, headers=ua, timeout=20)
    t = r.text
    print(f'=== {label} (size={len(t)}) ===')
    # CF challenge 키워드
    markers = ['cf-challenge', 'cf_chl_opt', 'Just a moment', '잠시만 기다려', 'Checking your browser', '__cf_bm', 'cloudflare']
    for m in markers:
        if m.lower() in t.lower():
            print(f'  [CF] marker: "{m}"')
    # 실제 컨텐츠 markers
    if '채용' in t or '구인' in t or 'job' in t.lower():
        print(f'  [OK] 채용/구인 키워드 있음')
    print(t[:800].replace('\n', ' ').replace('  ', ' ')[:800])
    print('---\n')
