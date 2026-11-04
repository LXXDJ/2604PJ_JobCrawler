"""사람인 — 키워드 검색 결과 HTML 파싱 (인증 불필요, 2026-11-03 확인).

  검색: GET /zf_user/search/recruit?searchword=&recruitPage=N&recruitPageCount=100&recruitSort=reg_dt
        → div.item_recruit[value=rec_idx] 100개/페이지
  상세: GET /zf_user/jobs/relay/view-detail?rec_idx=  (공고 페이지 안 iframe 본문)
        → .user_content. 약 절반은 이미지 통짜 공고라 텍스트가 거의 없음 → images 로 따로 보존

검색은 본문까지 매칭되므로 넓게 받고 profile.match 의 제목 필터로 좁힌다.
"""
from __future__ import annotations

import re
import time
from datetime import date
from typing import Iterable, Iterator

from bs4 import BeautifulSoup
from curl_cffi import requests

from .. import profile
from . import base
from .base import ListItem

SOURCE = "saramin"
FULL_LIST = False           # 키워드 검색 최신순 일부만 보므로 "리스트에 없음 = 마감" 판단 불가
BASE = "https://www.saramin.co.kr"
PAGE_SIZE = 100
MAX_PAGES = 10              # 키워드당 최신 1,000건까지 (첫 수집 기준. 이후엔 새 공고 없는 페이지에서 멈춤)

_SESSION: requests.Session | None = None


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session(impersonate="chrome124")
    return _SESSION


def parse_years(text: str) -> tuple[int | None, int | None]:
    """'경력 5~16년' / '경력 3년↑' / '신입' / '신입·경력' / '경력무관' → (from, to). to 100 = 무관."""
    t = (text or "").replace(" ", "")
    if "무관" in t:
        return 0, 100
    m = re.search(r"(\d+)~(\d+)년", t)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return (0 if "신입" in t else lo), hi
    m = re.search(r"(\d+)년↑|(\d+)년이상", t)
    if m:
        lo = int(m.group(1) or m.group(2))
        return (0 if "신입" in t else lo), 100
    if "신입" in t and "경력" in t:
        return 0, 100
    if "신입" in t:
        return 0, 0
    if "경력" in t:
        return 1, 100
    return None, None


def parse_due(text: str, today: date | None = None) -> str | None:
    """'~ 11/18(수)' → 'YYYY-11-18'. '상시채용'/'채용시' → None. 연도는 가장 가까운 미래로."""
    today = today or date.today()
    t = (text or "").strip()
    if "오늘마감" in t:
        return today.isoformat()
    m = re.search(r"(\d{1,2})/(\d{1,2})", t)
    if not m:
        return None
    mon, day = int(m.group(1)), int(m.group(2))
    year = today.year + (1 if mon < today.month - 1 else 0)   # 12월에 본 '~ 01/10' 은 다음 해
    try:
        return date(year, mon, day).isoformat()
    except ValueError:
        return None


def _parse_list(html: str) -> list[ListItem]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for it in soup.select("div.item_recruit"):
        rid = it.get("value")
        a = it.select_one("h2.job_tit a")
        if not rid or not a:
            continue
        corp = it.select_one(".corp_name a")
        csn = re.search(r"csn=([^&]+)", corp.get("href", "")) if corp else None
        cond = [s.get_text(" ", strip=True) for s in it.select(".job_condition > span")]
        career = next((c for c in cond if "경력" in c or "신입" in c), "")
        yf, yt = parse_years(career)
        due = it.select_one(".job_date .date")
        sectors = [x.get_text(strip=True) for x in it.select(".job_sector a")]
        out.append(ListItem(
            external_id=rid,
            url=f"{BASE}/zf_user/jobs/relay/view?rec_idx={rid}",
            title=a.get("title") or a.get_text(strip=True),
            company=corp.get_text(strip=True) if corp else "",
            company_id=csn.group(1) if csn else None,
            location=cond[0] if cond else None,
            years_from=yf, years_to=yt,
            due_date=parse_due(due.get_text(strip=True) if due else ""),
            category_ids=sectors,
            # 사람인 직무 키워드(딥러닝·머신러닝 등)는 일반 개발 공고에도 많이 붙어 있어 AI 판정에 쓰지 않음
            ai_category=False,
        ))
    return out


def iter_list(sleep_sec: float = 1.0, max_pages: int | None = None,
              known_ids: Iterable[str] = ()) -> Iterator[ListItem]:
    """키워드별 최신순. 한 페이지가 전부 이미 아는 공고면 그 키워드는 멈춘다 (매일 배치는 1~2페이지로 끝남)."""
    s = _session()
    known = set(known_ids)
    for kw in profile.SEARCH_KEYWORDS:
        for page in range(1, (max_pages or MAX_PAGES) + 1):
            r = base.get(s, f"{BASE}/zf_user/search/recruit", params={
                "searchword": kw, "searchType": "search", "recruitPage": page,
                "recruitPageCount": PAGE_SIZE, "recruitSort": "reg_dt",
            })
            items = _parse_list(r.text)
            fresh = [x for x in items if x.external_id not in known]
            yield from items
            known.update(x.external_id for x in items)
            time.sleep(sleep_sec)
            if len(items) < PAGE_SIZE or not fresh:
                break


def fetch_company(csn: str) -> dict:
    """회사 페이지 요약 카드 — 업력·설립 / 기업형태 / 사원수(국민연금) / 매출액(NICE)."""
    r = base.get(_session(), f"{BASE}/zf_user/company-info/view", params={"csn": csn})
    soup = BeautifulSoup(r.text, "html.parser")
    out = {"name": None, "founded_year": None, "tags": [], "open_positions": None, "response_rate": None,
           "employees": None, "revenue_eok": None, "op_income_eok": None, "revenue_growth": None,
           "company_type": None}
    title = soup.select_one("h1.tit_company, .company_name, title")
    if title:
        out["name"] = title.get_text(strip=True).split(" 20")[0]
    for li in soup.select("li.company_summary_item"):
        texts = [x.get_text(" ", strip=True) for x in li.find_all(["strong", "p"]) if x.get_text(strip=True)]
        joined = " ".join(texts)
        if "설립" in joined:
            m = re.search(r"(\d{4})년", joined)
            out["founded_year"] = int(m.group(1)) if m else None
        elif "기업형태" in joined:
            out["company_type"] = texts[0]
        elif "사원수" in joined:
            m = re.search(r"([\d,]+)", texts[0])
            n = int(m.group(1).replace(",", "")) if m else 0
            out["employees"] = n or None   # 0명 = 국민연금 미가입·정보 없음
        elif "매출액" in joined:
            out["revenue_eok"] = base.parse_krw_eok(texts[0])
    return out


def search_companies(name: str) -> list[tuple[str, str]]:
    """회사명 검색 → [(표시 이름, csn)]. 원티드 회사의 재무를 보충할 때 사용."""
    r = base.get(_session(), f"{BASE}/zf_user/search/company", params={"searchword": name})
    soup = BeautifulSoup(r.text, "html.parser")
    out, seen = [], set()
    for a in soup.select("a[href*='company-info/view?csn=']"):
        m = re.search(r"csn=([^&]+)", a.get("href", ""))
        label = a.get_text(" ", strip=True)
        if m and label and m.group(1) not in seen:
            seen.add(m.group(1))
            out.append((label, m.group(1)))
    return out


def fetch_detail(external_id: str) -> dict:
    s = _session()
    r = base.get(s, f"{BASE}/zf_user/jobs/relay/view-detail",
                 params={"rec_idx": external_id, "rec_seq": 0})
    soup = BeautifulSoup(r.text, "html.parser")
    body = soup.select_one(".user_content") or soup.body
    text = body.get_text("\n", strip=True) if body else ""
    images = [img.get("src") for img in (body.select("img") if body else []) if img.get("src")]
    return {
        "intro": None, "main_tasks": None, "requirements": None, "preferred": None, "benefits": None,
        "skill_tags": [],
        "body": text or None,
        "images": images,
        "status": "active",
    }
