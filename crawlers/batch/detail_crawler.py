"""상세 페이지 fetch → 본문 데이터 손실 없이 수집.

추출 대상 (모두 raw 에 저장):
- title             : <title> 또는 <h1>
- snippet           : 본문 plain text (BODY_MAX_CHARS 까지)
- body_html         : 본문 영역 inner HTML (BODY_HTML_MAX_CHARS 까지) — 포맷/표 보존
- images            : <img> + srcset + data-* lazy attrs (WP 사이즈 변형 dedupe)
- links             : 본문 내 <a href> + 앵커 텍스트
- attachments       : 파일 확장자(.pdf/.hwp/.doc/.xlsx/.zip 등) 링크 분리
- iframes           : <iframe src> (YouTube/Google Form 등 임베드)
- videos            : <video src> + <video><source src>
- emails / phones   : 본문 텍스트 정규식
- tables            : <table> 행렬 (자격요건/처우 같은 표)
- meta              : <meta name|property> 모두 (OG/Twitter/article:* 포함)
- jsonld            : <script type="application/ld+json"> 파싱 (JobPosting 등)

설계 원칙: 잃어버리는 데이터 없도록 body_html 원본도 함께 저장. 구조화 필드는
편의용. 다음에 새 필드 추출이 필요해지면 body_html 에서 재파싱 가능.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..fetchers.static import fetch


BODY_SELECTORS = (
    # gnuboard5 — bo_v_atc 가 본문(bo_v_con) + 이미지(bo_v_img) + 첨부영역을 모두 포함.
    # bo_v_con 만 쓰면 sibling 인 bo_v_img 의 본문 첨부 이미지가 빠짐.
    "#bo_v_atc", ".bo_v_atc", "#bo_v_con", ".bo_v_con",
    # 한국 사이트 흔한 게시판 view 컨테이너
    ".__boardView", ".board_view", ".board-view", ".bbs_view", ".bbs-view",
    ".view_content", ".view-content", ".view_cont", ".board-content",
    ".cont_view", ".board_cont", ".board_content",
    "article",
    ".entry-content",
    ".post-content",
    ".article-content",
    # jobposting.co.kr 계열
    ".body-body-", ".body-center-", ".job-body-",
    # hrdkorea / 한국 정부 사이트 계열
    "#print", "#normal_page", "#contents",
    # cambojob.com — left column 만 본문, right 는 사이드바
    ".job-detail-left", ".job-detail",
    # worldjob.or.kr — view-tab-wrap 이 진짜 detail
    ".view-tab-wrap", ".wjobDerailveiw",
    # peoplenjob.com — job-info 가 본문 (회사 카드/AI 요약/공유 모달 제외)
    ".job-info", ".jd-v2-card",
    "main",
    "#content",
    "#main",
)

# 본문이 잡히기 전에 통째로 제거할 사이트 보일러플레이트 (사이트 안내, 이전/다음글 등)
NOISE_SELECTORS = (
    ".__recHead", ".__boardPn",          # mofa.go.kr 계열
    ".boardPn", ".board_pn", ".bbs_pn", ".bbs-pn",
    ".prev-next", ".prevnext", ".pager", ".paging", ".pagination",
    ".notice-area", ".board-notice", ".disclaimer", ".caution",
    ".sns-share", ".share-area", ".btn-area", ".button-area",
    "#snb", "#lnb", "#gnb",  # 사이드/상단 nav 메뉴
    "#header", "#footer", "#left", "#right",  # div 기반 layout (hrdkorea 등)
    ".header", ".footer", ".footer-part1", ".footer-part2",  # class 기반 footer
    ".job-detail-right", ".categoryJobs", "#categoryJobs",  # cambojob 사이드바/추천
    # peoplenjob.com — AI 영문 요약 / 공유 모달 / 안내문
    "#ai-summary-section", ".ai-job-reference", ".ajr-foot",
    "#modal-share-label", ".modal-share", ".share-modal",
    ".job-detail__list",  # 하단 안내문 ("본 정보는 ... 피플앤잡 ...")
    # gnuboard5 — 댓글, 이전/다음글, 사용자 버튼
    "#bo_vc", ".bo_vc", "#bo_v_top", "#bo_v_btm",
    ".bo_v_top", ".bo_v_btm", ".btn_bo_user", ".bo_v_nb", "#bo_v_nb",
    "#bo_v_share", ".bo_v_share",
    # gnuboard5 modern — 작성자 정보 (등록일/조회수/...) + 이전/다음글 메타
    "#bo_v_info", ".bo_v_info", "#bo_v_meta", ".bo_v_meta",
    "#bo_v_link", ".bo_v_link",  # 이전/다음 글 링크
    "#bo_v_data", ".bo_v_data",  # 관련자료/이전/다음 — sibling 추출 후 제거
    # SNS 공유
    ".sns_share", ".sns-share-area", ".share-buttons", ".share_buttons",
    ".social-share", ".social_share", "#sns_share_modal", ".sns_share_modal",
    # jobposting.co.kr footer
    ".bottom_menu", ".bottom-menu--", ".copyright",
)
BODY_MAX_CHARS = 20_000          # plain text snippet 상한
BODY_HTML_MAX_CHARS = 200_000    # 200KB — 본문 HTML 원본
MAX_IMAGES = 200
MAX_LINKS = 200
MAX_TABLES = 50
MAX_TABLE_CELLS = 500
MAX_JSONLD_BYTES = 500_000

# WP responsive image suffix: filename-WIDTHxHEIGHT.ext → filename.ext
_WP_SIZE_RE = re.compile(r"-\d+x\d+(?=\.[a-zA-Z]{2,5}(?:\?|$))")
_FILE_EXTS = (
    "pdf|hwp|hwpx|doc|docx|xls|xlsx|ppt|pptx|zip|rar|7z|txt|csv|"
    "jpg|jpeg|png|gif|svg|webp|mp4|mov|avi"
)
_FILE_EXT_RE = re.compile(rf"\.({_FILE_EXTS})(?:\?|$)", re.IGNORECASE)
_FILE_EXT_TEXT_RE = re.compile(rf"\.({_FILE_EXTS})\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE_RE = re.compile(
    r"(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?)\d{3,4}[\s-]?\d{4}"
)


@dataclass
class JobDetail:
    url: str
    title: Optional[str] = None
    company: Optional[str] = None
    deadline: Optional[str] = None
    posted_at: Optional[str] = None
    raw_text_snippet: Optional[str] = None
    body_html: Optional[str] = None
    images: list[str] = field(default_factory=list)
    links: list[dict] = field(default_factory=list)
    attachments: list[dict] = field(default_factory=list)
    iframes: list[str] = field(default_factory=list)
    videos: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    tables: list[list[list[str]]] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    jsonld: list = field(default_factory=list)
    error: Optional[str] = None

    @property
    def ok(self) -> bool:
        return self.error is None


COMMENT_SELECTORS = (
    "#comments", ".comments", ".comments-area", ".comment-list",
    "ol.commentlist", "#respond", ".comment-respond", ".pingback",
    ".reply", "#disqus_thread", ".fb-comments",
)


def _strip_comments(soup: BeautifulSoup) -> None:
    for sel in COMMENT_SELECTORS:
        for node in soup.select(sel):
            node.decompose()


_TRAILING_NAV_RE = re.compile(
    r"(\s*(이전글|다음글|목록|이전\s*글|다음\s*글|prev\s*post|next\s*post|"
    r"댓글목록|comments?|share|좋아요|추천|"
    r"SNS\s*공유|관련자료|등록된\s*댓글이?\s*없습니다\.?|"
    r"로그인한?\s*회원만\s*댓글\s*등록이?\s*가능합니다\.?|"
    r"이전\s+[^|]+?\s*작성일\s*\d{4}[\.\-]\d{1,2}[\.\-]\d{1,2}[^|]*?|"
    r"다음\s+[^|]+?\s*작성일\s*\d{4}[\.\-]\d{1,2}[\.\-]\d{1,2}[^|]*?|"
    r"댓글\s*\d+|×)\s*)+$",
    re.IGNORECASE,
)


def _strip_trailing_nav(text: str) -> str:
    """본문 끝에 남는 nav 키워드 제거 (이전글/다음글/목록 등)."""
    return _TRAILING_NAV_RE.sub("", text).rstrip()


def _expand_html_textareas(soup: BeautifulSoup) -> int:
    """`<textarea>` 안에 escaped HTML 이 들어있는 케이스를 다시 파싱해서 자리 교체.

    jobposting.co.kr 처럼 본문이 hidden textarea 의 value 로 저장되고 JS 가 다른 div 에
    innerHTML 로 넣는 사이트 — textarea.text 만 뽑으면 HTML 태그가 그대로 텍스트가 됨.
    """
    n = 0
    for ta in soup.find_all("textarea"):
        content = ta.string if ta.string else ta.get_text()
        if not content or "<" not in content or ">" not in content:
            continue
        try:
            inner = BeautifulSoup(content, "html.parser")
        except Exception:  # noqa: BLE001
            continue
        # textarea 를 inner 의 모든 자식으로 교체
        ta.replace_with(inner)
        n += 1
    return n


def _pick_body_node(soup: BeautifulSoup) -> Optional[Tag]:
    for sel in BODY_SELECTORS:
        node = soup.select_one(sel)
        if node and len(node.get_text(strip=True)) >= 30:
            return node
    return None


def _gnuboard_extras(soup: BeautifulSoup) -> tuple[list[Tag], list[Tag]]:
    """gnuboard5 의 #bo_v_img (이미지), #bo_v_file/.bo_v_file (첨부) 노드들 반환.
    body_node 가 #bo_v_con (좁은 본문) 으로 잡혔을 때, 같은 article 안의 sibling
    이미지/첨부 영역이 누락되지 않도록 명시적으로 합치기 위함.
    """
    img_nodes = [n for n in soup.select("#bo_v_img, .bo_v_img") if n]
    file_nodes = [n for n in soup.select("#bo_v_file, .bo_v_file") if n]
    return img_nodes, file_nodes


def _abs(base: str, src: str) -> str:
    src = (src or "").strip()
    if not src or src.startswith(("data:", "javascript:", "#")):
        return ""
    return urljoin(base, src)


def _absolutize_html(node: Tag, base_url: str) -> None:
    """body_html 안의 상대 URL 들을 절대 URL 로 변환 (iframe 렌더 시 깨짐 방지)."""
    for tag in node.find_all(["a", "link"], href=True):
        u = _abs(base_url, tag.get("href"))
        if u:
            tag["href"] = u
    for tag in node.find_all(["img", "iframe", "video", "source", "audio", "embed",
                              "track", "script"], src=True):
        u = _abs(base_url, tag.get("src"))
        if u:
            tag["src"] = u
    for tag in node.find_all(["img", "source"]):
        srcset = tag.get("srcset")
        if srcset:
            new_parts = []
            for p in srcset.split(","):
                p = p.strip()
                if not p:
                    continue
                bits = p.split(None, 1)
                u = _abs(base_url, bits[0])
                if u:
                    new_parts.append(u + (" " + bits[1] if len(bits) > 1 else ""))
            if new_parts:
                tag["srcset"] = ", ".join(new_parts)
    for tag in node.find_all("form", action=True):
        u = _abs(base_url, tag.get("action"))
        if u:
            tag["action"] = u


def _canonical_image(u: str) -> str:
    return _WP_SIZE_RE.sub("", u)


def _extract_images(node: Tag, base_url: str) -> list[str]:
    out: list[str] = []
    seen_canon: set[str] = set()
    for img in node.find_all("img"):
        candidates = [
            img.get("src"),
            img.get("data-src"),
            img.get("data-lazy-src"),
            img.get("data-original"),
        ]
        srcset = img.get("srcset") or ""
        if srcset:
            candidates.extend(p.strip().split(" ")[0] for p in srcset.split(","))
        for c in candidates:
            u = _abs(base_url, c or "")
            if not u:
                continue
            canon = _canonical_image(u)
            if canon in seen_canon:
                continue
            seen_canon.add(canon)
            out.append(canon)
            if len(out) >= MAX_IMAGES:
                return out
    return out


def _extract_links_and_attachments(
    node: Tag, base_url: str
) -> tuple[list[dict], list[dict]]:
    links: list[dict] = []
    attachments: list[dict] = []
    seen: set[str] = set()
    for a in node.find_all("a", href=True):
        u = _abs(base_url, a.get("href"))
        if not u or u in seen:
            continue
        seen.add(u)
        text = " ".join(a.get_text(" ", strip=True).split())[:200]
        ext = ""
        # URL ext 검사는 path 부분만 — query 안의 fake .jpg 등 오인 방지
        # (e.g. view_image.php?fn=X.jpg → path 는 view_image.php → 매치 안 됨)
        from urllib.parse import urlparse as _up
        u_path = _up(u).path
        m = _FILE_EXT_RE.search(u_path)
        if m:
            ext = m.group(1).lower()
        else:
            tm = _FILE_EXT_TEXT_RE.search(text)
            if tm:
                ext = tm.group(1).lower()
            elif (("/download" in u.lower() or "download.php" in u.lower()
                   or "download.do" in u.lower())
                  and ("다운로드" in text or "download" in text.lower())):
                # 다운로드 안내 anchor text 가 있는 경우만 — view_image.php 같은
                # 'view' 페이지를 첨부로 오인하지 않기 위함
                ext = "bin"
        if ext:
            attachments.append({"url": u, "text": text, "ext": ext})
        if len(links) < MAX_LINKS:
            links.append({"url": u, "text": text})
    return links, attachments


def _extract_iframes_videos(node: Tag, base_url: str) -> tuple[list[str], list[str]]:
    iframes: list[str] = []
    videos: list[str] = []
    for ifr in node.find_all("iframe", src=True):
        u = _abs(base_url, ifr.get("src"))
        if u and u not in iframes:
            iframes.append(u)
    for v in node.find_all("video"):
        for src_attr in (v.get("src"),):
            u = _abs(base_url, src_attr or "")
            if u and u not in videos:
                videos.append(u)
        for s in v.find_all("source", src=True):
            u = _abs(base_url, s.get("src"))
            if u and u not in videos:
                videos.append(u)
    return iframes, videos


def _extract_tables(node: Tag) -> list[list[list[str]]]:
    out: list[list[list[str]]] = []
    for tbl in node.find_all("table"):
        if len(out) >= MAX_TABLES:
            break
        rows: list[list[str]] = []
        cells_seen = 0
        for tr in tbl.find_all("tr"):
            cells = [
                " ".join(c.get_text(" ", strip=True).split())
                for c in tr.find_all(["td", "th"])
            ]
            if cells:
                rows.append(cells)
                cells_seen += len(cells)
                if cells_seen >= MAX_TABLE_CELLS:
                    break
        if rows:
            out.append(rows)
    return out


def _extract_meta(soup: BeautifulSoup) -> dict:
    meta: dict = {}
    for m in soup.find_all("meta"):
        key = m.get("property") or m.get("name") or m.get("itemprop")
        val = m.get("content")
        if key and val:
            meta[str(key)] = str(val)[:1000]
    return meta


def _extract_jsonld(soup: BeautifulSoup) -> list:
    out: list = []
    total = 0
    for s in soup.find_all("script", attrs={"type": "application/ld+json"}):
        text = (s.string or s.get_text() or "").strip()
        if not text:
            continue
        total += len(text)
        if total > MAX_JSONLD_BYTES:
            break
        try:
            out.append(json.loads(text))
        except Exception:  # noqa: BLE001
            out.append({"_raw": text[:5000], "_parse_error": True})
    return out


def _extract_contacts(text: str) -> tuple[list[str], list[str]]:
    emails = list(dict.fromkeys(_EMAIL_RE.findall(text)))[:50]
    phones_raw = _PHONE_RE.findall(text)
    phones = list(dict.fromkeys(p.strip() for p in phones_raw if len(p.strip()) >= 9))[:50]
    return emails, phones


def _resolve_synthetic_url(url: str, *, timeout: int) -> Optional[str]:
    """`?_jsfn=fn&_jsid=N` 합성 URL → 실제 detail URL 로 리라이트.

    list_extractor 가 `javascript:goview('16811')` 같은 JS 핸들러를
    `?_jsfn=goview&_jsid=16811` 합성 형태로 저장한다. 이 함수는 부모 JSP 페이지를
    한 번 받아서 해당 JS 함수의 `f.action="X"` 를 찾아 실제 URL 로 변환한다.
    실제 detail 로 매핑할 수 없으면 (popup 류 등) None 반환 → fetch_detail 에서 error.
    """
    from urllib.parse import urlparse, parse_qs, urljoin
    p = urlparse(url)
    qs = parse_qs(p.query)
    fn = (qs.get("_jsfn") or [None])[0]
    jsid = (qs.get("_jsid") or [None])[0]
    if not fn or not jsid:
        return url

    # 사이트 특화 — worldjob.or.kr 의 goView1 함수는 view.do 로 가지만 그건
    # 직접 접근 시 'pageAction alert' 차단됨. 실제 detail 본문은
    # epmtLinkMyday.do 에서 동일 params 로 200KB+ 응답.
    if "worldjob.or.kr" in p.netloc and fn.lower() == "goview1":
        return (f"https://www.worldjob.or.kr/advnc/epmtLinkMyday.do"
                f"?joCrtfcNo={jsid}&joCrtfcDsp=1&joCrtfcDspSn=1&dobType=1")

    # popup-suffix fn 은 navigation 이 아니라 모달/팝업 — detail 페이지 없음.
    # (예: busiInfoPopup = 회사정보 팝업, list 컨테이너 안에 detail anchor 와
    #  나란히 등장하는 경우 list_extractor 가 잘못 채택할 수 있음.)
    if fn.lower().endswith("popup"):
        return None

    parent_url = f"{p.scheme}://{p.netloc}{p.path}"
    pr = fetch(parent_url, timeout=timeout)
    if not pr.ok:
        return None
    fn_re = re.compile(
        rf"function\s+{re.escape(fn)}\s*\([^)]*\)\s*\{{([^}}]+)\}}",
        re.IGNORECASE,
    )
    m = fn_re.search(pr.text or "")
    if not m:
        return None
    body = m.group(1)
    action_m = re.search(r'\.action\s*=\s*[\'"]([^\'"]+)[\'"]', body)
    if not action_m:
        return None
    action = action_m.group(1)
    # idx 변수 이름 (form 의 input name) 추출 — 변수가 f/frm/obj/form 등 다양
    idx_m = re.search(r'\w+\.(\w+)\.value\s*=', body)
    idx_name = idx_m.group(1) if idx_m else "idx"
    action_abs = urljoin(parent_url, action)
    sep = "&" if "?" in action_abs else "?"
    return f"{action_abs}{sep}{idx_name}={jsid}"


def fetch_detail(url: str, *, timeout: int = 20) -> JobDetail:
    real_url = _resolve_synthetic_url(url, timeout=timeout)
    if real_url is None:
        return JobDetail(url=url, error="synthetic URL not resolvable to real detail")
    r = fetch(real_url, timeout=timeout)
    if not r.ok:
        return JobDetail(url=real_url, error=r.error or f"HTTP {r.status}")

    soup = BeautifulSoup(r.text, "html.parser")

    # 1) <head> 영역 데이터 (decompose 전)
    title = None
    if soup.title and soup.title.string:
        title = soup.title.string.strip()
    elif h1 := soup.find("h1"):
        title = h1.get_text(strip=True)
    meta = _extract_meta(soup)
    jsonld = _extract_jsonld(soup)

    # 2) 노이즈 태그 제거 후 본문 영역만 작업
    for t in soup(["script", "style", "noscript", "nav", "header", "footer", "aside"]):
        t.decompose()
    _strip_comments(soup)
    # textarea 안 escaped HTML 재파싱 (jobposting 등) — strip 보다 먼저 해야 그 안의 내용도 strip 적용됨
    _expand_html_textareas(soup)

    # NOISE strip 전에 gnuboard5 sibling 추출 (이미지/첨부 노드들 — 이후 본문 추출 시 합쳐짐)
    extra_imgs, extra_files = _gnuboard_extras(soup)
    extra_imgs = [BeautifulSoup(str(n), "html.parser") for n in extra_imgs]
    extra_files = [BeautifulSoup(str(n), "html.parser") for n in extra_files]

    for sel in NOISE_SELECTORS:
        for n in soup.select(sel):
            n.decompose()

    body_node = _pick_body_node(soup) or soup
    body_text = " ".join(body_node.get_text(" ", strip=True).split())[:BODY_MAX_CHARS]
    body_text = _strip_trailing_nav(body_text)
    _absolutize_html(body_node, url)
    body_html = str(body_node)[:BODY_HTML_MAX_CHARS]

    images = _extract_images(body_node, url)
    links, attachments = _extract_links_and_attachments(body_node, url)
    iframes, videos = _extract_iframes_videos(body_node, url)
    tables = _extract_tables(body_node)

    # gnuboard5 — body_node 가 #bo_v_con 처럼 좁게 잡혔을 때, sibling 인
    # #bo_v_img / #bo_v_file 의 이미지·첨부 anchor 도 합쳐서 추출.
    # (extra_imgs / extra_files 는 NOISE strip 전에 미리 추출해둔 사본)
    for n in extra_imgs:
        for u in _extract_images(n, url):
            if u not in images and len(images) < MAX_IMAGES:
                images.append(u)
    for n in extra_files:
        _, more_atts = _extract_links_and_attachments(n, url)
        existing_urls = {a["url"] for a in attachments}
        for a in more_atts:
            if a["url"] not in existing_urls:
                attachments.append(a)

    emails, phones = _extract_contacts(body_text)

    # 빈 detail 가드: 본문/이미지/첨부/jsonld 모두 사실상 없으면 에러로 취급.
    # (worldjob 처럼 만료된 job ID 가 홈페이지로 redirect 되거나 anti-scraping
    #  에 걸려 placeholder HTML 이 떨어진 경우 — title 만 남는 garbage 방지.)
    if (len(body_text) < 50 and not images and not attachments
            and not iframes and not videos and not jsonld):
        return JobDetail(url=real_url,
                         error=f"empty detail (body={len(body_text)}c, no media/jsonld)")

    return JobDetail(
        url=real_url,
        title=title,
        raw_text_snippet=body_text,
        body_html=body_html,
        images=images,
        links=links,
        attachments=attachments,
        iframes=iframes,
        videos=videos,
        emails=emails,
        phones=phones,
        tables=tables,
        meta=meta,
        jsonld=jsonld,
    )
