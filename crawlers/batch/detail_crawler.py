"""상세 페이지 fetch → 최소 메타 추출.

지금은 minimal: title 만 추출 (이미 list 에서 갖고 있는 title 도 활용).
회사/마감일/게시일 등은 사이트마다 구조가 천차만별이라 v2에서 LLM 또는
사이트별 추출기를 붙일 예정.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from bs4 import BeautifulSoup

from ..fetchers.static import fetch


@dataclass
class JobDetail:
    url: str
    title: Optional[str] = None
    company: Optional[str] = None
    deadline: Optional[str] = None
    posted_at: Optional[str] = None
    raw_text_snippet: Optional[str] = None
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


def fetch_detail(url: str, *, timeout: int = 20) -> JobDetail:
    r = fetch(url, timeout=timeout)
    if not r.ok:
        return JobDetail(url=url, error=r.error or f"HTTP {r.status}")

    soup = BeautifulSoup(r.text, "html.parser")

    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    elif h1 := soup.find("h1"):
        title = h1.get_text(strip=True)

    # 본문 텍스트 일부 (raw payload 용)
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    body_text = " ".join(soup.get_text(" ", strip=True).split())[:1000]

    return JobDetail(
        url=url,
        title=title,
        raw_text_snippet=body_text,
    )
