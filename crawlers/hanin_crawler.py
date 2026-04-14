"""
재캄보디아한인회 구인구직 게시판 크롤러
- 사이트: http://www.hanin.or.kr/bbs/board.php?bo_table=Information
- 플랫폼: 그누보드 (나리야 테마)
- 봇 탐지: 없음 (robots.txt 404)
"""

import requests
from bs4 import BeautifulSoup
import json
import time
import os
from datetime import datetime


# ============================================================
# 1단계: HTTP 요청 기초
# ============================================================
# requests 라이브러리로 웹페이지의 HTML을 가져온다.
# 브라우저가 하는 일을 코드로 하는 것:
#   브라우저: URL 입력 → 서버에 요청 → HTML 받음 → 화면에 렌더링
#   크롤러:  URL 입력 → 서버에 요청 → HTML 받음 → 코드로 파싱

BASE_URL = "http://www.hanin.or.kr"
BOARD_URL = f"{BASE_URL}/bbs/board.php"
BOARD_TABLE = "Information"  # 구인구직 게시판 식별자

# User-Agent: 서버에게 "나는 브라우저야"라고 알려주는 헤더
# 이걸 안 보내면 일부 사이트에서 봇으로 판단하고 차단함
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}


def fetch_page(url, params=None):
    """
    웹페이지 HTML을 가져오는 함수.

    - requests.get(): HTTP GET 요청을 보낸다
    - verify=False: SSL 인증서 검증 비활성화 (이 사이트는 self-signed 인증서 사용)
    - response.text: 받아온 HTML 문자열
    """
    response = requests.get(url, params=params, headers=HEADERS, verify=False, timeout=10)
    response.raise_for_status()  # HTTP 에러(4xx, 5xx) 발생 시 예외 발생
    return response.text


# ============================================================
# 2단계: HTML 파싱 - 목록 페이지에서 게시글 정보 추출
# ============================================================
# BeautifulSoup: HTML 문자열을 파이썬 객체로 변환해서
# 태그, 클래스, 속성 등으로 원하는 데이터를 쉽게 찾을 수 있게 해준다.
#
# 이 게시판의 HTML 구조:
#   <li class="d-md-table-row ...">           ← 게시글 1개
#     <div class="na-item">
#       <a class="na-subject" href="...">제목</a>  ← 제목 + 링크
#     </div>
#     <span class="sv_member">작성자</span>        ← 작성자
#     (날짜 텍스트)                                ← 등록일
#     (조회수 텍스트)                              ← 조회수
#   </li>

def parse_list_page(html):
    """목록 페이지 HTML에서 게시글 목록을 추출한다."""
    soup = BeautifulSoup(html, "lxml")

    # select(): CSS 셀렉터로 요소를 찾는다
    # "ul.na-table > li" = na-table 클래스를 가진 ul 태그의 직계 자식 li 태그들
    rows = soup.select("ul.na-table > li")

    posts = []
    for row in rows:
        # 제목과 링크 추출
        subject_tag = row.select_one("a.na-subject")
        if not subject_tag:
            continue

        title = subject_tag.get_text(strip=True)
        link = subject_tag.get("href", "")

        # wr_id 추출 (게시글 고유 번호) - 중복 체크에 사용
        wr_id = ""
        if "wr_id=" in link:
            wr_id = link.split("wr_id=")[-1].split("&")[0]

        # 작성자 추출
        author_tag = row.select_one("span.sv_member")
        author = author_tag.get_text(strip=True) if author_tag else ""

        # 날짜, 조회수 추출 - sr-only(스크린리더 전용) 라벨 다음의 텍스트
        date = ""
        hit = ""
        cells = row.select("div.d-md-table-cell")
        for cell in cells:
            sr = cell.select_one("span.sr-only")
            if sr:
                label = sr.get_text(strip=True)
                # sr-only 태그 이후의 텍스트가 실제 값
                value = cell.get_text(strip=True).replace(label, "").strip()
                if label == "등록일":
                    date = value
                elif label == "조회":
                    hit = value

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
# 3단계: 상세 페이지 크롤링
# ============================================================
# 목록에서 얻은 링크로 각 게시글의 상세 내용을 가져온다.
# 상세 페이지 구조:
#   <article id="bo_v">
#     <div id="bo_v_con">
#       <div class="view-content">본문 내용</div>
#     </div>
#   </article>

def parse_detail_page(html):
    """상세 페이지 HTML에서 본문 내용을 추출한다."""
    soup = BeautifulSoup(html, "lxml")

    content_div = soup.select_one("div.view-content")
    if not content_div:
        return ""

    # get_text(): HTML 태그를 제거하고 텍스트만 추출
    # separator="\n": 태그 사이에 줄바꿈 삽입
    # strip=True: 앞뒤 공백 제거
    return content_div.get_text(separator="\n", strip=True)


# ============================================================
# 4단계: 페이지네이션 처리
# ============================================================
# 이 게시판의 페이지 URL 패턴:
#   page=1, page=2, page=3 ...
# 현재 전체 14건이라 1페이지뿐이지만, 향후 증가를 대비

def get_total_page_count(html):
    """목록 페이지에서 전체 페이지 수를 파악한다."""
    soup = BeautifulSoup(html, "lxml")

    # "전체 14 / 1 페이지" 텍스트에서 파악
    total_info = soup.select_one("#bo_list_total")
    if total_info:
        text = total_info.get_text(strip=True)
        # "전체 14 / 1 페이지" 형태
        if "페이지" in text:
            parts = text.split("/")
            if len(parts) >= 2:
                page_part = parts[-1].strip().replace("페이지", "").strip()
                try:
                    return int(page_part)
                except ValueError:
                    pass

    # 페이지네이션 링크에서 마지막 페이지 번호 추출
    page_links = soup.select("a[href*='page=']")
    max_page = 1
    for link in page_links:
        href = link.get("href", "")
        if "page=" in href:
            try:
                page_num = int(href.split("page=")[-1].split("&")[0])
                max_page = max(max_page, page_num)
            except ValueError:
                pass
    return max_page


# ============================================================
# 5단계: 전체 크롤링 실행 + 데이터 저장
# ============================================================

def crawl_all():
    """전체 크롤링 실행: 목록 → 상세 → JSON 저장"""
    print("=" * 60)
    print("재캄보디아한인회 구인구직 크롤러 시작")
    print("=" * 60)

    all_posts = []

    # 1) 첫 페이지를 가져와서 전체 페이지 수 확인
    print("\n[1] 목록 페이지 가져오는 중...")
    first_page_html = fetch_page(BOARD_URL, params={
        "bo_table": BOARD_TABLE,
        "page": 1,
    })
    total_pages = get_total_page_count(first_page_html)
    print(f"    전체 페이지 수: {total_pages}")

    # 2) 모든 페이지 순회하며 목록 수집
    for page in range(1, total_pages + 1):
        print(f"\n[2] {page}/{total_pages} 페이지 목록 수집 중...")

        if page == 1:
            html = first_page_html  # 이미 가져온 첫 페이지 재사용
        else:
            html = fetch_page(BOARD_URL, params={
                "bo_table": BOARD_TABLE,
                "page": page,
            })
            time.sleep(1)  # 서버 부담 줄이기 (예의 바른 크롤링)

        posts = parse_list_page(html)
        print(f"    게시글 {len(posts)}건 발견")
        all_posts.extend(posts)

    print(f"\n    총 {len(all_posts)}건의 게시글 수집 완료")

    # 3) 각 게시글의 상세 페이지 크롤링
    print("\n[3] 상세 페이지 크롤링 중...")
    for i, post in enumerate(all_posts):
        print(f"    ({i+1}/{len(all_posts)}) {post['title'][:40]}...")

        detail_html = fetch_page(post["link"])
        post["content"] = parse_detail_page(detail_html)
        post["crawled_at"] = datetime.now().isoformat()

        time.sleep(1)  # 요청 사이에 1초 대기

    # 4) JSON 파일로 저장
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    output_path = os.path.join(data_dir, "hanin_jobs.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_posts, f, ensure_ascii=False, indent=2)

    print(f"\n[4] 저장 완료: {output_path}")
    print(f"    총 {len(all_posts)}건")
    print("=" * 60)

    return all_posts


if __name__ == "__main__":
    # SSL 경고 무시 (self-signed 인증서 때문)
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    crawl_all()
