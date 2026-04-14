"""
그누보드 기반 사이트 범용 크롤러

사이트마다 테마(CSS 클래스)가 다르지만, 그누보드의 기본 구조는 동일하다:
  - 목록: board.php?bo_table=XXX&page=N
  - 상세: board.php?bo_table=XXX&wr_id=N
  - 게시글 고유 ID: wr_id

이 크롤러는 config.py의 설정(CSS 셀렉터 등)을 받아서
어떤 그누보드 사이트든 크롤링할 수 있다.
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import os
from datetime import datetime

# SSL 경고 무시 (self-signed 인증서 사이트용)
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


# ============================================================
# HTTP 요청
# ============================================================

def fetch_page(url, params=None, verify_ssl=True):
    """웹페이지 HTML을 가져온다."""
    response = requests.get(
        url, params=params, headers=HEADERS,
        verify=verify_ssl, timeout=10,
    )
    response.raise_for_status()
    return response.text


# ============================================================
# 목록 페이지 파싱 — parse_mode에 따라 분기
# ============================================================

def parse_list_page(html, config):
    """
    목록 페이지에서 게시글 목록을 추출한다.

    config의 parse_mode에 따라 날짜/조회수 추출 방식이 달라진다:
    - "sr_only" : 스크린리더 라벨(sr-only) 기반 → 한인회 나리야 테마
    - "direct"  : CSS 클래스로 직접 접근 → 시엠립 fz 테마
    """
    soup = BeautifulSoup(html, "lxml")
    sel = config["selectors"]

    rows = soup.select(sel["list_rows"])

    posts = []
    for row in rows:
        # 헤더 행(th) 건너뛰기
        if row.select_one(".fz_list_th, .na-table-head"):
            continue
        # 클래스에 fz_list_th가 있으면 건너뛰기
        row_classes = row.get("class", [])
        if "fz_list_th" in row_classes:
            continue

        # 제목 + 링크
        subject_tag = row.select_one(sel["subject_link"])
        if not subject_tag:
            continue

        # 아이콘/배지 태그 제거 후 제목 텍스트만 추출
        # 그누보드 테마마다 제목 안에 아이콘 span이 섞여있음
        # 예: <span class="icon_pack2">텍스트</span>, <span class="cnt_cmt">1</span>
        for icon in subject_tag.select("span.icon_pack, span.icon_pack2, span.cnt_cmt, span.sound_only"):
            icon.decompose()

        title = subject_tag.get_text(strip=True)
        link = subject_tag.get("href", "")

        # 상대경로 → 절대경로 변환
        if link.startswith("/"):
            link = config["base_url"] + link

        # wr_id 추출
        wr_id = ""
        if "wr_id=" in link:
            wr_id = link.split("wr_id=")[-1].split("&")[0]

        # 작성자
        author_tag = row.select_one(sel["author"])
        author = author_tag.get_text(strip=True) if author_tag else ""

        # 날짜 & 조회수 — parse_mode에 따라 다르게 추출
        date = ""
        hit = ""

        if config["parse_mode"] == "sr_only":
            # 나리야 테마: sr-only 라벨 다음 텍스트
            cells = row.select(sel["date"])
            for cell in cells:
                sr = cell.select_one("span.sr-only")
                if sr:
                    label = sr.get_text(strip=True)
                    value = cell.get_text(strip=True).replace(label, "").strip()
                    if label == "등록일":
                        date = value
                    elif label == "조회":
                        hit = value

        elif config["parse_mode"] == "direct":
            # fz 테마: 클래스로 직접 접근
            date_tag = row.select_one(sel["date"])
            hit_tag = row.select_one(sel["hit"])
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
            "source": config["name"],
        })

    return posts


# ============================================================
# 상세 페이지 파싱
# ============================================================

def parse_detail_page(html, config):
    """상세 페이지에서 본문을 추출한다."""
    soup = BeautifulSoup(html, "lxml")
    sel = config["selectors"]

    content_div = soup.select_one(sel["content"])
    if not content_div:
        return ""

    return content_div.get_text(separator="\n", strip=True)


# ============================================================
# 페이지 수 파악
# ============================================================

def get_total_page_count(html, config):
    """목록 페이지에서 전체 페이지 수를 파악한다."""
    soup = BeautifulSoup(html, "lxml")

    # 페이지네이션 링크에서 최대 페이지 번호 추출
    page_links = soup.select("a[href*='page=']")
    max_page = 1
    for a_tag in page_links:
        href = a_tag.get("href", "")
        if "page=" in href:
            try:
                page_num = int(href.split("page=")[-1].split("&")[0])
                max_page = max(max_page, page_num)
            except ValueError:
                pass
    return max_page


# ============================================================
# 단일 사이트 크롤링
# ============================================================

def crawl_site(config):
    """설정(config)을 받아서 해당 사이트를 크롤링한다."""
    name = config["name"]
    desc = config["description"]

    print(f"\n{'='*60}")
    print(f"[{name}] {desc} 크롤링 시작")
    print(f"{'='*60}")

    all_posts = []

    # 1) 첫 페이지 → 전체 페이지 수 확인
    print(f"\n  [1] 목록 페이지 가져오는 중...")
    first_html = fetch_page(
        config["board_url"],
        params={"bo_table": config["board_table"], "page": 1},
        verify_ssl=config["verify_ssl"],
    )
    total_pages = get_total_page_count(first_html, config)
    print(f"      전체 페이지 수: {total_pages}")

    # 2) 모든 페이지 순회
    for page in range(1, total_pages + 1):
        print(f"  [2] {page}/{total_pages} 페이지 수집 중...")

        if page == 1:
            html = first_html
        else:
            html = fetch_page(
                config["board_url"],
                params={"bo_table": config["board_table"], "page": page},
                verify_ssl=config["verify_ssl"],
            )
            time.sleep(1)

        posts = parse_list_page(html, config)
        print(f"      게시글 {len(posts)}건 발견")
        all_posts.extend(posts)

    print(f"\n      총 {len(all_posts)}건 수집")

    # 3) 상세 페이지 크롤링
    print(f"\n  [3] 상세 페이지 크롤링 중...")
    for i, post in enumerate(all_posts):
        print(f"      ({i+1}/{len(all_posts)}) {post['title'][:40]}...")

        detail_html = fetch_page(
            post["link"], verify_ssl=config["verify_ssl"],
        )
        post["content"] = parse_detail_page(detail_html, config)
        post["crawled_at"] = datetime.now().isoformat()

        time.sleep(1)

    return all_posts


# ============================================================
# 전체 실행: 모든 사이트 크롤링 → 저장
# ============================================================

def crawl_all(sites):
    """여러 사이트를 순회하며 크롤링하고 결과를 저장한다."""
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    total_count = 0

    for config in sites:
        posts = crawl_site(config)
        total_count += len(posts)

        # 사이트별 JSON 저장
        output_path = os.path.join(data_dir, f"{config['name']}_jobs.json")
        with open(output_path, "w", encoding="utf-8") as f:
            json.dump(posts, f, ensure_ascii=False, indent=2)

        print(f"\n  [4] 저장 완료: {output_path} ({len(posts)}건)")

    print(f"\n{'='*60}")
    print(f"전체 크롤링 완료: 총 {total_count}건 ({len(sites)}개 사이트)")
    print(f"{'='*60}")


if __name__ == "__main__":
    from config import SITES
    crawl_all(SITES)
