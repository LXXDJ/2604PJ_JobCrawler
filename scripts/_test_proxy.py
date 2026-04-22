"""Webshare 스타일 프록시 작동 확인."""
import sys
import requests

sys.stdout.reconfigure(encoding='utf-8')

PROXY = "http://lscbspwf:5vvmixnb5mw9@31.59.20.176:6754"
proxies = {"http": PROXY, "https": PROXY}
ua = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"}

tests = [
    ("출구 IP 확인", "https://httpbin.org/ip"),
    ("네이버", "https://www.naver.com/"),
    ("리멤버 (CF 차단됐던 곳)", "https://career.rememberapp.co.kr/"),
    ("자소설닷컴", "https://jasoseol.com/recruit"),
    ("슈퍼루키", "https://www.superookie.com/"),
    ("헤이코리안", "https://www.heykorean.com/"),
    ("호주나라", "https://www.hojunara.com/"),
]

for label, url in tests:
    try:
        r = requests.get(url, proxies=proxies, headers=ua, timeout=20, allow_redirects=True)
        print(f'  {label:30s} [{r.status_code}] size={len(r.text)} final={r.url[:80]}')
    except Exception as e:
        print(f'  {label:30s} ✗ {type(e).__name__}: {str(e)[:100]}')
