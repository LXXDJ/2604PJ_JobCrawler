"""curl DNS 실패로 skip 처리된 도메인을 다양한 방법으로 재확인.

- socket.getaddrinfo (시스템 DNS)
- dnspython 으로 8.8.8.8 / 1.1.1.1 직접 질의
- 실제 HTTP GET 시도 (브라우저 UA)

curl 이 실패해도 브라우저/다른 DNS 로는 해결되는 경우가 흔하다.
"""
import socket
import sys

sys.stdout.reconfigure(encoding='utf-8')

TARGETS = [
    # DNS 죽음으로 스킵한 것들
    "www.contentjob.co.kr",
    "www.teacherjob.co.kr",
    "www.medijob.co.kr",
    "www.overseajob.co.kr",
    "www.researchjob.co.kr",
    "www.productionjob.co.kr",
    "www.koreaboston.com",
    "www.beijingkorea.com",
    "www.vietnamhani.com",
    "www.cebuhanin.com",
    "www.hawaiihanin.com",
    "www.hongkonghanin.org",
    # connection fail (DNS resolve 는 됐지만 포트 접속 실패)
    "japan.or.kr",
    "www.otwojob.com",
]

def system_dns(host):
    try:
        ip = socket.gethostbyname(host)
        return f"✓ {ip}"
    except socket.gaierror as e:
        return f"✗ {e}"

def public_dns(host, resolver_ip):
    try:
        import dns.resolver
        r = dns.resolver.Resolver()
        r.nameservers = [resolver_ip]
        r.lifetime = 5
        ans = r.resolve(host, 'A')
        return f"✓ {ans[0]}"
    except ImportError:
        return "? (dnspython 없음)"
    except Exception as e:
        return f"✗ {type(e).__name__}"

def http_get(host):
    try:
        import requests
        r = requests.get(
            f"https://{host}",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            timeout=10,
            allow_redirects=True,
        )
        return f"HTTP {r.status_code} → {r.url[:60]}"
    except Exception as e:
        return f"✗ {type(e).__name__}: {str(e)[:80]}"

print(f'{"도메인":<28} {"로컬DNS":<20} {"8.8.8.8":<15} {"HTTP":<40}')
print("-" * 110)
for host in TARGETS:
    local = system_dns(host)
    public = public_dns(host, "8.8.8.8")
    http = http_get(host)
    print(f'{host:<28} {local:<20} {public:<15} {http:<40}')
