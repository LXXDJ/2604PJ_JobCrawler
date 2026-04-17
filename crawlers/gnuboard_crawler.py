"""
그누보드 범용 크롤러 (DB 저장 + 증분 수집)

Analyzer가 생성한 config를 받아서 그누보드 사이트를 크롤링한다.
목록 → 상세 2단계로 크롤링하고, DB에 (source, external_id)로 중복 체크하여 upsert.

config 구조 (analyzer가 생성):
{
    "platform": "gnuboard",
    "base_url": "...",
    "board_table": "...",
    "theme": "nariya" | "fz",
    "selectors": {
        "list_rows": "...",
        "subject_link": "...",
        "author": "...",
        "date": "...",        (direct 모드에서만)
        "hit": "...",         (direct 모드에서만)
        "content": "...",
    },
    "parse_mode": "sr_only" | "direct"
}
"""

import time
import sys
from bs4 import BeautifulSoup

sys.path.insert(0, __file__.rsplit("\\", 1)[0].rsplit("/", 1)[0] + "/crawlers")

from database import JobDatabase
from http_client import fetch


# ============================================================
# 목록 페이지 파싱
# ============================================================

def parse_list_page(html, config):
    """목록 페이지에서 게시글 행들을 추출한다 (parse_mode에 따라 분기)."""
    soup = BeautifulSoup(html, "lxml")
    sel = config["selectors"]
    parse_mode = config.get("parse_mode", "sr_only")

    rows = soup.select(sel["list_rows"])

    posts = []
    for row in rows:
        # 헤더 행 건너뛰기
        row_classes = row.get("class") or []
        if any(cls in row_classes for cls in ["fz_list_th", "na-table-head"]):
            continue
        if row.select_one(".fz_list_th, .na-table-head"):
            continue

        # 제목 + 링크
        subject_tag = row.select_one(sel["subject_link"])
        if not subject_tag:
            continue

        # 아이콘/배지 span 제거 (예: <span class="icon_pack2">텍스트</span>)
        for icon in subject_tag.select(
            "span.icon_pack, span.icon_pack2, span.cnt_cmt, span.sound_only"
        ):
            icon.decompose()

        title = subject_tag.get_text(strip=True)
        link = subject_tag.get("href", "")
        if link.startswith("/"):
            link = config["base_url"] + link

        # wr_id 추출 (게시글 고유 ID)
        wr_id = ""
        if "wr_id=" in link:
            wr_id = link.split("wr_id=")[-1].split("&")[0]

        # 작성자
        author_tag = row.select_one(sel["author"])
        author = author_tag.get_text(strip=True) if author_tag else ""

        # 날짜 & 조회수 — parse_mode에 따라
        date = ""
        hit = ""

        if parse_mode == "sr_only":
            # nariya 테마: sr-only 라벨 다음 텍스트
            for cell in row.select("div.d-md-table-cell"):
                sr = cell.select_one("span.sr-only")
                if sr:
                    label = sr.get_text(strip=True)
                    value = cell.get_text(strip=True).replace(label, "").strip()
                    if label == "등록일":
                        date = value
                    elif label == "조회":
                        hit = value

        elif parse_mode == "direct":
            # fz 테마: 클래스로 직접 접근
            date_tag = row.select_one(sel.get("date", ""))
            hit_tag = row.select_one(sel.get("hit", ""))
            if date_tag:
                date = date_tag.get_text(strip=True)
            if hit_tag:
                hit = hit_tag.get_text(strip=True)

        posts.append({
            "wr_id": wr_id,
            "title": title,
            "author": author,
            "date": date,
            "hit": hit,
            "link": link,
        })

    return posts


# ============================================================
# 상세 페이지 파싱
# ============================================================

def parse_detail_page(html, config):
    """상세 페이지에서 본문을 추출한다."""
    soup = BeautifulSoup(html, "lxml")
    content_tag = soup.select_one(config["selectors"].get("content", ""))
    if not content_tag:
        return ""
    return content_tag.get_text(separator="\n", strip=True)


# ============================================================
# 페이지 수 파악
# ============================================================

def get_total_pages(html):
    """페이지네이션 링크에서 마지막 페이지 번호 추출"""
    soup = BeautifulSoup(html, "lxml")
    page_links = soup.select("a[href*='page=']")
    max_page = 1
    for a in page_links:
        href = a.get("href", "")
        try:
            page_num = int(href.split("page=")[-1].split("&")[0])
            max_page = max(max_page, page_num)
        except ValueError:
            pass
    return max_page


# ============================================================
# 메인 크롤링 함수
# ============================================================

def crawl(site_id, config, db_path, max_pages=None, http_config=None):
    """
    그누보드 사이트 크롤링 + DB 저장.

    site_id: DB에 저장될 source 식별자 ('hanin', 'siemreap' 등)
    config: 사이트 설정 dict (analyzer가 생성한 형식)
    db_path: SQLite DB 파일 경로
    max_pages: 최대 페이지 수 (None이면 전체)
    http_config: {"timeout", "max_retries", "retry_backoff"}
    """
    http_config = http_config or {}
    http_kwargs = {
        "timeout": http_config.get("timeout", 30),
        "max_retries": http_config.get("max_retries", 3),
        "retry_backoff": http_config.get("retry_backoff", 2.0),
    }

    print("=" * 60)
    print(f"[{site_id}] 그누보드 크롤러 시작")
    print(f"    base_url: {config['base_url']}")
    print(f"    board: {config['board_table']} (theme: {config.get('theme')})")
    print("=" * 60)

    # DB 준비
    db = JobDatabase(db_path)
    db.init_schema()
    run_id = db.start_crawl_run(site_id)

    new_count = 0
    updated_count = 0
    error_msg = None

    board_url = f"{config['base_url']}/bbs/board.php"

    try:
        # 1) 첫 페이지 가져오기 → 전체 페이지 수 파악
        first_html = fetch(
            board_url,
            params={"bo_table": config["board_table"], "page": 1},
            **http_kwargs,
        )
        total_pages = get_total_pages(first_html)

        if max_pages:
            total_pages = min(total_pages, max_pages)
        print(f"\n    수집할 페이지: {total_pages}")

        # 2) 모든 페이지 순회
        for page in range(1, total_pages + 1):
            html = first_html if page == 1 else fetch(
                board_url,
                params={"bo_table": config["board_table"], "page": page},
                **http_kwargs,
            )
            if page > 1:
                time.sleep(1)

            posts = parse_list_page(html, config)
            print(f"\n[{page}/{total_pages}] {len(posts)}건 처리 중...")

            # 3) 각 게시글 처리 (DB 체크 후 신규면 상세 크롤링)
            for post in posts:
                wr_id = post["wr_id"]
                if not wr_id:
                    continue

                existing = _check_exists(db, site_id, wr_id)

                job = {
                    "source": site_id,
                    "external_id": wr_id,
                    "title": post["title"],
                    "company": post["author"],  # 게시판엔 '작성자' 개념
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
                else:
                    # 신규 — 상세 페이지 크롤링
                    try:
                        detail_html = fetch(post["link"], **http_kwargs)
                        job["content"] = parse_detail_page(detail_html, config)
                        time.sleep(0.5)
                    except Exception as e:
                        print(f"      [WARN] 상세 페이지 실패 ({wr_id}): {e}")

                    db.upsert_job(job)
                    new_count += 1
                    print(f"      [NEW] {post['title'][:60]}")

            print(f"    누적: 신규 {new_count}, 기존 {updated_count}")

        db.finish_crawl_run(run_id, new_count, updated_count)

    except Exception as e:
        error_msg = str(e)
        db.finish_crawl_run(run_id, new_count, updated_count, error=error_msg)
        raise

    print(f"\n{'=' * 60}")
    print(f"[{site_id}] 크롤링 완료 — 신규 {new_count}건 / 재확인 {updated_count}건")
    print(f"{'=' * 60}")

    return {"new": new_count, "updated": updated_count}


def _check_exists(db, source, external_id):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id),
        ).fetchone()
        return row is not None
