"""Playwright 로 URL 로드 후 DOM 상태 덤프 (디버그용)."""
import sys, os
from playwright.sync_api import sync_playwright

url = sys.argv[1]
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    ctx = browser.new_context(ignore_https_errors=True)
    page = ctx.new_page()
    try:
        page.goto(url, wait_until="commit", timeout=45000)
    except Exception as e:
        print(f"goto commit 실패: {e}")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception as e:
        print(f"domcontentloaded 실패: {e}")
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception as e:
        print(f"networkidle timeout: {e}")
    page.wait_for_timeout(5000)  # 추가 5초 렌더 대기
    stats = page.evaluate("""() => ({
        anchors: document.querySelectorAll('a[href]').length,
        navs: document.querySelectorAll('nav').length,
        headers: document.querySelectorAll('header').length,
        gnb: document.querySelectorAll('.gnb, [class*="gnb"], [class*="Gnb"], [class*="GNB"]').length,
        menu: document.querySelectorAll('[class*="menu"], [class*="Menu"]').length,
        bodyText: (document.body ? document.body.innerText.length : 0),
        readyState: document.readyState,
    })""")
    print(stats)
    browser.close()
