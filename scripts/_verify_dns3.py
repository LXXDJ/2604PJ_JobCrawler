import sys
import dns.resolver
import requests

sys.stdout.reconfigure(encoding='utf-8')

CHECKS = [
    ("otwojob.com", None),  # 루트 도메인
    ("otwojob.com", "A"),
    ("japan.or.kr", None),
]

r = dns.resolver.Resolver(configure=False)
r.nameservers = ['8.8.8.8']
r.lifetime = 5

for host, _ in CHECKS:
    try:
        ans = r.resolve(host, 'A')
        print(f'{host:25s} DNS A: {ans[0]}')
    except Exception as e:
        print(f'{host:25s} DNS FAIL: {type(e).__name__}')

print()
for host in ['otwojob.com', 'japan.or.kr']:
    for proto in ['https', 'http']:
        url = f'{proto}://{host}/'
        try:
            r2 = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0"},
                timeout=10,
                allow_redirects=True,
            )
            print(f'{url:40s} {r2.status_code} → {r2.url[:70]}')
            break
        except Exception as e:
            print(f'{url:40s} FAIL {type(e).__name__}: {str(e)[:60]}')
