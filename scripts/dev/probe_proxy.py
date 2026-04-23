"""프록시 IP 에 대해 일반적인 포트/프로토콜 조합으로 연결 테스트.

발급받은 정보가 IP만 있는 상태 — 포트, 프로토콜, 인증 여부를 탐지.
"""
import socket
import sys

sys.stdout.reconfigure(encoding='utf-8')

IP = "31.59.20.176"
PORTS = [80, 443, 3128, 8080, 8888, 8000, 1080, 8118, 8081, 9050, 9150, 1085]

print(f'=== 포트 스캔: {IP} ===\n')
open_ports = []
for port in PORTS:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect((IP, port))
        print(f'  {port:>5}: OPEN')
        open_ports.append(port)
    except socket.timeout:
        print(f'  {port:>5}: timeout')
    except ConnectionRefusedError:
        print(f'  {port:>5}: refused')
    except Exception as e:
        print(f'  {port:>5}: {type(e).__name__}')
    finally:
        s.close()

if not open_ports:
    print('\n→ 열린 포트 없음. 인증 없는 공개 프록시가 아닐 가능성.')
    sys.exit(0)

print(f'\n=== 열린 포트로 HTTP 프록시 시도: {open_ports} ===\n')
import requests

for port in open_ports:
    for scheme in ('http', 'socks5h', 'socks5'):
        proxy_url = f'{scheme}://{IP}:{port}'
        proxies = {'http': proxy_url, 'https': proxy_url}
        try:
            r = requests.get('https://httpbin.org/ip', proxies=proxies, timeout=10)
            print(f'  ✓ {proxy_url:<35} → {r.status_code}  body: {r.text[:120]}')
        except requests.exceptions.ProxyError as e:
            msg = str(e)[:80]
            print(f'  ✗ {proxy_url:<35} ProxyError: {msg}')
        except Exception as e:
            print(f'  ✗ {proxy_url:<35} {type(e).__name__}: {str(e)[:60]}')
