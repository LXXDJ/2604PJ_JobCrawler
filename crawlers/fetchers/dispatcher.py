"""정적/동적 fetcher 라우팅.

정책:
  - mode='static'  : 정적만 (기본, 빠름)
  - mode='dynamic' : 동적만 (SPA 사이트 — 등록 시점에 결정되어 source 에 기록)
  - mode='auto'    : 정적 → list 후보 0개 또는 빈 페이지면 동적 fallback (등록 단계만)
"""
from __future__ import annotations

from .static import FetchResult, fetch as fetch_static


def _looks_empty_or_spa(text: str) -> bool:
    """정적 응답이 SPA 셸이거나 의미 있는 콘텐츠가 거의 없는지 휴리스틱 판단."""
    if not text:
        return True
    low = text.lower()
    # SPA 마커
    spa_markers = ["__nuxt__", "__next_data__", 'id="root"', 'id="app"',
                   "ng-app", "vue", "react-dom"]
    has_spa_marker = any(m in low for m in spa_markers)
    # 페이지 안의 link 수가 매우 적으면 (nav 정도만) SPA 셸일 가능성
    n_a = low.count("<a ")
    if has_spa_marker and n_a < 30:
        return True
    if n_a < 5:
        return True
    return False


def fetch(url: str, *, mode: str = "static", **kw) -> FetchResult:
    if mode == "dynamic":
        from .dynamic import fetch as fetch_dynamic
        return fetch_dynamic(url, **kw)

    # static or auto
    r = fetch_static(url, **{k: v for k, v in kw.items()
                              if k in ("timeout", "impersonate", "headers")})
    if mode == "auto" and (not r.ok or _looks_empty_or_spa(r.text)):
        from .dynamic import fetch as fetch_dynamic
        rd = fetch_dynamic(url)
        if rd.ok:
            return rd
    return r
