import sys
import dns.resolver

sys.stdout.reconfigure(encoding='utf-8')

HOSTS = [
    'www.contentjob.co.kr', 'www.teacherjob.co.kr', 'www.medijob.co.kr',
    'www.overseajob.co.kr', 'www.researchjob.co.kr', 'www.productionjob.co.kr',
    'www.koreaboston.com', 'www.beijingkorea.com', 'www.vietnamhani.com',
    'www.cebuhanin.com', 'www.hawaiihanin.com', 'www.hongkonghanin.org',
    'japan.or.kr', 'www.otwojob.com',
    # 대조군 — 정상 도메인
    'www.naver.com', 'www.jobkorea.co.kr',
]

for resolver_ip in ['8.8.8.8', '1.1.1.1']:
    print(f'\n=== Resolver: {resolver_ip} ===')
    r = dns.resolver.Resolver(configure=False)
    r.nameservers = [resolver_ip]
    r.lifetime = 5
    for h in HOSTS:
        try:
            a = r.resolve(h, 'A')
            print(f'  {h:30s} OK   {a[0]}')
        except dns.resolver.NXDOMAIN:
            print(f'  {h:30s} NXDOMAIN (도메인 없음)')
        except dns.resolver.NoAnswer:
            print(f'  {h:30s} NoAnswer (A 레코드 없음)')
        except dns.resolver.Timeout:
            print(f'  {h:30s} Timeout')
        except Exception as e:
            print(f'  {h:30s} {type(e).__name__}: {e}')
