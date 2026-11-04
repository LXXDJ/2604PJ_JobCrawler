"""잡코리아 — 검색 페이지(Next.js)의 RSC payload 안 react-query 데이터를 JSON 으로 꺼냄 (2026-11-03 확인).

  검색: GET /Search/?stext=&tabType=recruit&Page_No=N&ord=RegDtDesc
        → self.__next_f.push([1,"..."]) 조각을 이어 붙이면 {"pageSize":20,...,"content":[공고...]} 가 들어 있음
        공고 필드: id, title, companyName, careerType(1 신입/2 경력/3 신입·경력/4 무관), careerRange(최소 연차, 100=무관),
                   applicationPeriod.end, jobClassificationOrIndustry(",AI/ML엔지니어,MLOps엔지니어,")
  상세: GET /Recruit/GI_Read_Comt_Ifrm?Gno=  (공고 페이지 안 iframe 본문) — 대부분 텍스트, 일부 이미지
"""
from __future__ import annotations

import json
import re
import time
from typing import Iterable, Iterator

from bs4 import BeautifulSoup
from curl_cffi import requests

from .. import profile
from . import base
from .base import ListItem

SOURCE = "jobkorea"
FULL_LIST = False           # 키워드 검색 최신순 일부만 보므로 "리스트에 없음 = 마감" 판단 불가
BASE = "https://www.jobkorea.co.kr"
PAGE_SIZE = 20              # 사이트 고정값 (pageSize 파라미터 무시됨)
MAX_PAGES = 30              # 키워드당 최신 600건까지 (첫 수집 기준. 이후엔 새 공고 없는 페이지에서 멈춤)

# 잡코리아 직무 분류 중 AI 직무 — 제목에 키워드가 없어도 통과
_AI_DUTY = re.compile(r"AI/ML엔지니어|프롬프트엔지니어")
_PUSH_RE = re.compile(r'self\.__next_f\.push\(\[1,"((?:[^"\\]|\\.)*)"\]\)')

COMPANY_SLEEP = 2.0         # 회사 페이지를 연달아 받으면 봇 확인으로 넘어감 (2026-11-03, 수백 건 0.3s 간격에서 발생)

_SESSION: requests.Session | None = None


class Blocked(RuntimeError):
    """봇 확인 페이지(jkConfirm.aspx)로 리다이렉트됨 — 이번 회차는 잡코리아 요청을 멈춰야 함."""


def _session() -> requests.Session:
    global _SESSION
    if _SESSION is None:
        _SESSION = requests.Session(impersonate="chrome124")
    return _SESSION


def _get(url: str, params: dict | None = None) -> requests.Response:
    r = base.get(_session(), url, params=params)
    if "jkConfirm" in str(r.url):
        raise Blocked(f"jobkorea 봇 확인 페이지: {url}")
    return r


def _search_data(html: str) -> dict | None:
    """RSC 조각 중 검색 결과(공고 목록) 객체. 같은 형태의 다른 목록(채용관 등)과 구분하려고 legacyJobNo 로 찾음."""
    payload = "".join(json.loads(f'"{m.group(1)}"') for m in _PUSH_RE.finditer(html))
    i = payload.find('"legacyJobNo"')
    if i < 0:
        return None
    start = payload.rfind('{"pageSize"', 0, i)
    if start < 0:
        return None
    obj, _ = json.JSONDecoder().raw_decode(payload[start:])
    return obj


def _years(career_type: str | None, career_range) -> tuple[int | None, int | None]:
    rng = career_range if isinstance(career_range, int) else None
    if career_type == "1":
        return 0, 0
    if career_type == "2":
        return (rng if rng is not None and rng < 100 else 1), 100
    if career_type in ("3", "4"):
        return 0, 100
    return None, None


def _to_item(c: dict) -> ListItem:
    yf, yt = _years(c.get("careerType"), c.get("careerRange"))
    end = ((c.get("applicationPeriod") or {}).get("end") or "")[:10]
    duty = c.get("jobClassificationOrIndustry") or ""
    loc = c.get("_internal_featureLocationCode") or ""
    return ListItem(
        external_id=str(c["id"]),
        url=f"{BASE}/Recruit/GI_Read/{c['id']}",
        title=c.get("title") or "",
        company=c.get("companyName") or c.get("postingCompanyName") or "",
        company_id=c.get("memberSystemNo"),
        location=" ".join(loc.split(",")[:2]) or None,
        years_from=yf, years_to=yt,
        due_date=end if end and end < "2060" else None,   # 상시채용은 2070-01-01·9999-… 같은 먼 날짜로 옴
        category_ids=[x for x in duty.split(",") if x],
        ai_category=bool(_AI_DUTY.search(duty)),
    )


def iter_list(sleep_sec: float = 1.0, max_pages: int | None = None,
              known_ids: Iterable[str] = ()) -> Iterator[ListItem]:
    """키워드별 등록일순. 한 페이지가 전부 이미 아는 공고면 그 키워드는 멈춘다."""
    known = set(known_ids)
    for kw in profile.SEARCH_KEYWORDS:
        for page in range(1, (max_pages or MAX_PAGES) + 1):
            r = _get(f"{BASE}/Search/", params={
                "stext": kw, "tabType": "recruit", "Page_No": page, "ord": "RegDtDesc",
            })
            data = _search_data(r.text) or {}
            items = [_to_item(c) for c in data.get("content") or [] if c.get("id")]
            fresh = [x for x in items if x.external_id not in known]
            yield from items
            known.update(x.external_id for x in items)
            time.sleep(sleep_sec)
            if len(items) < PAGE_SIZE or not fresh or page >= (data.get("totalPages") or 0):
                break


def fetch_company(member_no: str) -> dict:
    """회사 페이지 /Recruit/Co_Read/C/{memberSystemNo} — 기업정보 표 + 재무 카드.
    브랜드관 운영 회사는 /Company/{id}/Info 로 리다이렉트되고 요약 카드(li > span.icnCorpNN + p) 형식이다."""
    r = _get(f"{BASE}/Recruit/Co_Read/C/{member_no}")
    soup = BeautifulSoup(r.text, "html.parser")
    fields = {}
    for th in soup.select("th.field-label"):
        td = th.find_next_sibling("td")
        val = td.select_one(".value") if td else None
        if val:
            fields[th.get_text(strip=True)] = val.get_text(" ", strip=True)
    for span in soup.select("li > span[class^=icnCorp]"):   # 브랜드관 형식
        ps = [p.get_text(" ", strip=True) for p in span.find_parent("li").find_all("p")]
        label = span.get_text(strip=True)
        if ps and label not in fields:
            fields[{"기업형태": "기업구분"}.get(label, label)] = " ".join(ps) if label == "설립일" else ps[0]
    text = re.sub(r"[ \t]+", " ", soup.get_text("\n", strip=True))
    op = re.search(r"\d{4}년 영업이익\n([^\n]+)", text)
    growth = re.search(r"매출\s*액\s*증가\s*율\n(-?[\d.]+)%", text)
    founded = re.search(r"(\d{4})", fields.get("설립일", ""))
    emp = re.search(r"([\d,]+)", fields.get("사원수", ""))
    name = soup.select_one("h1, .company-name, title")
    return {
        "name": name.get_text(strip=True)[:60] if name else None,
        "founded_year": int(founded.group(1)) if founded else None,
        "tags": [], "open_positions": None, "response_rate": None,
        "employees": int(emp.group(1).replace(",", "")) if emp else None,
        "revenue_eok": base.parse_krw_eok(fields.get("매출액")),
        "op_income_eok": base.parse_krw_eok(op.group(1)) if op else None,
        "revenue_growth": float(growth.group(1)) if growth else None,
        "company_type": fields.get("기업구분"),
    }


def fetch_detail(external_id: str) -> dict:
    r = _get(f"{BASE}/Recruit/GI_Read_Comt_Ifrm", params={"Gno": external_id})
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style"]):
        t.decompose()
    text = soup.get_text("\n", strip=True)
    images = [img.get("src") for img in soup.select("img") if img.get("src")]
    return {
        "intro": None, "main_tasks": None, "requirements": None, "preferred": None, "benefits": None,
        "skill_tags": [],
        "body": text or None,
        "images": images,
        "status": "active",
    }
