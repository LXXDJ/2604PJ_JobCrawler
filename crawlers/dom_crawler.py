"""
범용 DOM 크롤러

config.extraction_method == "dom" 인 모든 사이트를 처리한다.
gnuboard_crawler 의 로직을 일반화 + 확장한 것.

핵심 차이 vs gnuboard_crawler:
    - URL 고정패턴 하드코딩 제거 → config.source.list_url + list_params + pagination 으로 조립
    - parse_mode (sr_only/direct) 는 유지 (Bootstrap 접근성 패턴이라 재활용 가능)
    - skip_row_if_has_class 로 헤더 행 필터 일반화
    - external_id 추출은 url param 기반으로 명시화 (config.source.external_id_from_url_param)

지원 범위 (Phase 1):
    - requires_render = false 만 (requests 로 HTML fetch)
    - pagination.type = "url_param" 만

향후 Phase 에서 requires_render=true (Playwright 렌더) + 다른 pagination 타입 추가.
"""

import time
from typing import Optional
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from database import JobDatabase
from http_client import fetch


# ============================================================
# 리스트/상세 파싱
# ============================================================

def _parse_list_page(html: str, config: dict) -> list:
    """리스트 페이지에서 게시글 행들 추출."""
    source = config["source"]
    selectors = source["selectors"]
    parse_mode = source.get("parse_mode", "direct")
    skip_classes = set(source.get("skip_row_if_has_class") or [])
    # 상세 링크 resolve 의 기준. list_url 이 있으면 그걸 기준으로 urljoin 해야
    # path-relative 링크 (view.php?... 같은 leading slash 없는 형태) 도 처리 가능.
    link_base = source.get("list_url") or source.get("base_url", "")

    soup = BeautifulSoup(html, "lxml")
    rows = soup.select(selectors["list_rows"])

    posts = []
    for row in rows:
        # 헤더 행 필터 (자체 클래스 OR 자손에 헤더 클래스 포함)
        row_classes = set(row.get("class") or [])
        if skip_classes & row_classes:
            continue
        if skip_classes and any(row.select_one(f".{cls}") for cls in skip_classes):
            continue

        subject_tag = row.select_one(selectors["subject_link"])
        if not subject_tag:
            continue

        # 아이콘/배지 span 제거 (gnuboard 에서 흔함)
        for icon in subject_tag.select(
            "span.icon_pack, span.icon_pack2, span.cnt_cmt, span.sound_only"
        ):
            icon.decompose()

        title = subject_tag.get_text(strip=True)
        link = subject_tag.get("href", "")
        # subject_link 이 h1/span 같은 anchor 내부 원소를 가리키는 경우 (radiokorea 등),
        # href 는 조상 <a> 에 있으므로 위로 올라가 찾는다.
        if not link:
            anchor = subject_tag.find_parent("a")
            if anchor:
                link = anchor.get("href", "")
        if link and link_base and not urlparse(link).scheme:
            link = urljoin(link_base, link)

        # 저자
        author = ""
        if selectors.get("author"):
            tag = row.select_one(selectors["author"])
            if tag:
                author = tag.get_text(strip=True)

        # 날짜/조회수 — parse_mode 별 분기
        date, hit = _extract_date_hit(row, selectors, parse_mode)

        posts.append({
            "title": title,
            "author": author,
            "date": date,
            "hit": hit,
            "link": link,
        })

    return posts


def _extract_date_hit(row, selectors: dict, parse_mode: str) -> tuple:
    """parse_mode 에 따라 date/hit 필드를 뽑는다."""
    if parse_mode == "sr_only":
        # nariya 등 접근성 Bootstrap 패턴:
        # 같은 셀(div.d-md-table-cell) 안에 <span class="sr-only">등록일</span>텍스트 형태
        date, hit = "", ""
        for cell in row.select("div.d-md-table-cell"):
            sr = cell.select_one("span.sr-only")
            if not sr:
                continue
            label = sr.get_text(strip=True)
            value = cell.get_text(strip=True).replace(label, "").strip()
            if label == "등록일":
                date = value
            elif label == "조회":
                hit = value
        return date, hit

    # direct: 각자 셀렉터로 직접
    date = ""
    hit = ""
    if selectors.get("date"):
        tag = row.select_one(selectors["date"])
        if tag:
            date = tag.get_text(strip=True)
    if selectors.get("hit"):
        tag = row.select_one(selectors["hit"])
        if tag:
            hit = tag.get_text(strip=True)
    return date, hit


def _parse_detail_page(html: str, config: dict, detail_url: str = "",
                       http_kwargs: Optional[dict] = None) -> str:
    """상세 페이지 본문 추출.

    selectors.content 로 본문을 뽑되, 매치 결과가 비어있고
    selectors.content_iframe 이 설정돼 있으면 해당 iframe src 를
    추가로 fetch 해 동일한 content selector 로 재시도한다.
    (incruit 처럼 본문이 iframe 안에 있는 사이트 대응.)
    """
    selectors = config["source"]["selectors"]
    content_sel = selectors.get("content")
    if not content_sel:
        return ""

    soup = BeautifulSoup(html, "lxml")
    tag = soup.select_one(content_sel)
    if tag:
        text = tag.get_text(separator="\n", strip=True)
        if text:
            return text

    iframe_sel = selectors.get("content_iframe")
    if iframe_sel and http_kwargs is not None:
        iframe = soup.select_one(iframe_sel)
        src = iframe.get("src") if iframe else None
        if src:
            base = detail_url or config["source"].get("base_url", "")
            iframe_url = urljoin(base, src)
            try:
                iframe_html = fetch(iframe_url, **http_kwargs)
            except Exception as e:
                print(f"      [WARN] iframe follow 실패: {type(e).__name__}: {e}")
                return ""
            inner = BeautifulSoup(iframe_html, "lxml").select_one(content_sel)
            if inner:
                return inner.get_text(separator="\n", strip=True)
    return ""


def _extract_external_id(link: str, config: dict) -> str:
    """
    링크 URL 에서 external_id 추출.

    config.source.external_id_from_url_param 이 지정되면 해당 쿼리 파라미터 값 사용.
    (예: gnuboard 는 'wr_id')

    지정 없으면 링크 전체를 ID 로 사용 (중복 체크용 hash 대체).
    """
    param_name = config["source"].get("external_id_from_url_param")
    if not param_name:
        return link  # 폴백 — 링크 자체를 ID 로
    try:
        qs = parse_qs(urlparse(link).query)
    except Exception:
        return link
    values = qs.get(param_name)
    return values[0] if values else link


# ============================================================
# 페이지네이션
# ============================================================

def _get_total_pages(html: str, pagination: dict) -> int:
    """
    pagination.type = 'url_param' 한정: 페이지 링크의 max N 을 추정.
    향후 다른 타입은 분기 추가.
    """
    if pagination.get("type") != "url_param":
        return 1

    param = pagination.get("param", "page")
    soup = BeautifulSoup(html, "lxml")
    page_links = soup.select(f"a[href*='{param}=']")

    max_page = 1
    for a in page_links:
        href = a.get("href", "")
        try:
            page_num = int(href.split(f"{param}=")[-1].split("&")[0])
            max_page = max(max_page, page_num)
        except ValueError:
            pass
    return max_page


def _build_list_params(config: dict, page_num: int) -> dict:
    """리스트 페이지 요청 파라미터 조립: fixed list_params + pagination 파라미터."""
    source = config["source"]
    pagination = config.get("pagination") or {}

    params = dict(source.get("list_params") or {})
    if pagination.get("type") == "url_param":
        params[pagination.get("param", "page")] = page_num
    return params


# ============================================================
# 메인 크롤링 진입점
# ============================================================

def crawl(
    site_id: str,
    config: dict,
    db_path: str,
    max_pages: Optional[int] = None,
    http_config: Optional[dict] = None,
):
    """
    DOM 기반 사이트 크롤링 + DB 저장.

    site_id: DB 의 source 식별자
    config:  sites.json 신 스키마 엔트리의 전체 dict (extraction_method/source/pagination 포함)
    db_path: SQLite DB 경로
    max_pages: 호출자가 지정한 최대 페이지 (없으면 config.pagination.max_pages 사용)
    http_config: {timeout, max_retries, retry_backoff}
    """
    if config.get("requires_render"):
        raise NotImplementedError(
            "requires_render=True 는 Phase 2 에서 지원 예정. "
            "지금은 정적 HTML 만 처리 가능."
        )

    source = config.get("source") or {}
    pagination = config.get("pagination") or {}
    list_url = source.get("list_url")
    if not list_url:
        raise ValueError(f"[{site_id}] config.source.list_url 이 비어있음")

    http_kwargs = {
        "timeout": (http_config or {}).get("timeout", 30),
        "max_retries": (http_config or {}).get("max_retries", 3),
        "retry_backoff": (http_config or {}).get("retry_backoff", 2.0),
    }

    print("=" * 60)
    print(f"[{site_id}] DOM 크롤러 시작")
    print(f"    list_url : {list_url}")
    print(f"    params   : {source.get('list_params')}")
    print(f"    parse_mode: {source.get('parse_mode', 'direct')}")
    print("=" * 60)

    db = JobDatabase(db_path)
    db.init_schema()
    run_id = db.start_crawl_run(site_id)

    new_count = 0
    updated_count = 0

    try:
        # 1) 첫 페이지 → 총 페이지 수 추정
        first_params = _build_list_params(config, pagination.get("start", 1))
        first_html = fetch(list_url, params=first_params, **http_kwargs)
        total_pages = _get_total_pages(first_html, pagination)

        # max_pages 캡: 호출자 파라미터 > config 지정값 > 전체 추정치
        cap = max_pages or pagination.get("max_pages")
        if cap:
            total_pages = min(total_pages, cap)

        # early termination: 연속으로 "이미 있음" 이 이 임계치만큼 나오면
        # 뒤 페이지는 긁지 않고 종료. 신규 발견 시 카운터 리셋되므로
        # bump(오래된 공고가 위로 끌어올려짐) 상황에도 뚫고 지나감.
        # 0 이면 비활성 (전체 순회).
        stop_threshold = pagination.get("consecutive_existing_stop", 30)
        consecutive_existing = 0

        print(f"\n    수집할 페이지: {total_pages}"
              + (f"  (연속 기존 {stop_threshold}건 시 조기종료)"
                 if stop_threshold else ""))

        # 2) 페이지 순회
        start_page = pagination.get("start", 1)
        for page in range(start_page, start_page + total_pages):
            if page == start_page:
                html = first_html
            else:
                params = _build_list_params(config, page)
                html = fetch(list_url, params=params, **http_kwargs)
                time.sleep(1)

            posts = _parse_list_page(html, config)
            print(f"\n[{page}/{start_page + total_pages - 1}] {len(posts)}건 처리 중...")

            for post in posts:
                ext_id = _extract_external_id(post["link"], config)
                if not ext_id:
                    continue

                existing = _check_exists(db, site_id, ext_id)
                job = {
                    "source": site_id,
                    "external_id": ext_id,
                    "title": post["title"],
                    "company": post["author"],
                    "location": "",
                    "salary": "",
                    "job_type": "",
                    "pub_date": post["date"],
                    "link": post["link"],
                    "content": "",
                    "raw_data": {"list": post},
                }

                if existing:
                    db.upsert_job(job)
                    updated_count += 1
                    consecutive_existing += 1
                else:
                    try:
                        detail_html = fetch(post["link"], **http_kwargs)
                        job["content"] = _parse_detail_page(
                            detail_html, config, post["link"], http_kwargs,
                        )
                        time.sleep(0.5)
                    except Exception as e:
                        print(f"      [WARN] 상세 페이지 실패 ({ext_id}): {e}")
                    db.upsert_job(job)
                    new_count += 1
                    consecutive_existing = 0
                    print(f"      [NEW] {post['title'][:60]}")

            print(f"    누적: 신규 {new_count}, 기존 {updated_count}"
                  f"  (연속 기존 {consecutive_existing})")

            if stop_threshold and consecutive_existing >= stop_threshold:
                print(f"    [조기종료] 연속 기존 {consecutive_existing} >= {stop_threshold}"
                      f" — page {page} 에서 중단")
                break

        db.finish_crawl_run(run_id, new_count, updated_count)

    except Exception as e:
        db.finish_crawl_run(run_id, new_count, updated_count, error=str(e))
        raise

    print(f"\n{'=' * 60}")
    print(f"[{site_id}] 크롤링 완료 — 신규 {new_count}건 / 재확인 {updated_count}건")
    print(f"{'=' * 60}")

    return {"new": new_count, "updated": updated_count}


def _check_exists(db, source: str, external_id: str) -> bool:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        ).fetchone()
        return row is not None
