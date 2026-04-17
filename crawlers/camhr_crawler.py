"""
CamHR 크롤러 (API 기반)
- 사이트: https://www.camhr.com/
- 방식: REST API 직접 호출 (브라우저 렌더링 불필요)
- API 엔드포인트: https://api.camhr.com/v1.0.0/jobs/simple/page-query
- 상세 API: https://api.camhr.com/v1.0.0/jobs/{id}

CamHR은 Nuxt.js(Vue SSR) 기반 SPA로, HTML에 데이터가 없고
JavaScript가 API를 호출해서 채용공고를 동적으로 로딩한다.

이런 사이트를 크롤링하는 방법은 2가지:
  1) Playwright/Selenium으로 브라우저를 띄워서 JS 실행 후 HTML 파싱
  2) API를 직접 찾아서 호출 (더 빠르고 효율적) ← 이 방식 사용

API를 찾는 방법:
  - 브라우저 개발자도구 > Network 탭에서 XHR/Fetch 요청 확인
  - 또는 Playwright로 네트워크 요청 캡처 (camhr_api_finder.py 참고)
"""

import requests
import json
import time
import os
import sys
from datetime import datetime

sys.stdout.reconfigure(encoding="utf-8")


# ============================================================
# API 설정
# ============================================================

API_BASE = "https://api.camhr.com/v1.0.0"
LIST_API = f"{API_BASE}/jobs/simple/page-query"
DETAIL_API = f"{API_BASE}/jobs"  # + /{job_id}

# 브라우저처럼 보이기 위한 헤더
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.camhr.com/",
    "Accept": "application/json",
}

# 한 페이지당 가져올 공고 수 (API 기본값 15, 최대 테스트 필요)
PAGE_SIZE = 15


# ============================================================
# 목록 API 호출
# ============================================================

def fetch_job_list(page=1, size=PAGE_SIZE):
    """
    채용공고 목록을 API로 가져온다.

    응답 구조:
    {
        "data": {
            "page": 1,
            "size": 15,
            "totalCount": "1807",
            "totalPage": 121,
            "result": [ { "id": "...", "title": "...", ... }, ... ]
        }
    }
    """
    response = requests.get(
        LIST_API,
        params={"page": page, "size": size},
        headers=HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


# ============================================================
# 상세 API 호출
# ============================================================

def fetch_job_detail(job_id):
    """
    채용공고 상세 정보를 API로 가져온다.

    응답에 requirement(자격요건), description(상세설명) 등
    목록에는 없는 상세 정보가 포함되어 있다.
    """
    response = requests.get(
        f"{DETAIL_API}/{job_id}",
        headers=HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


# ============================================================
# 데이터 정제
# ============================================================

def parse_list_item(item):
    """목록 API 응답의 각 항목을 정제한다."""
    employer = item.get("employer", {})
    salary = item.get("salaryId", {})
    term = item.get("termId", {})

    return {
        "id": item.get("id", ""),
        "title": item.get("title", ""),
        "company": employer.get("company", ""),
        "employer_id": employer.get("employerId", ""),
        "cities": item.get("cities", ""),
        "salary": salary.get("label", ""),
        "term": term.get("label", ""),  # Full Time, Part Time 등
        "is_urgent": item.get("isurgent", False),
        "is_new": item.get("newJob", False),
        "pub_date": item.get("pubdate", ""),
        "link": f"https://www.camhr.com/a/job/{item.get('id', '')}",
        "source": "camhr",
    }


def parse_detail(data):
    """상세 API 응답에서 추가 정보를 추출한다."""
    detail = data.get("data", {})
    return {
        "requirement": detail.get("requirement", ""),
        "description": detail.get("description", ""),
        "address": detail.get("address", ""),
        "web_url": detail.get("weburl", ""),
        "hirelings": detail.get("hirelings", 0),  # 채용 인원
        "work_years": detail.get("workyears", 0),  # 경력 요구
        "close_date": detail.get("closeDate", ""),
    }


# ============================================================
# 전체 크롤링 실행
# ============================================================

def crawl_all(max_pages=None):
    """
    CamHR 전체 크롤링.

    max_pages: 크롤링할 최대 페이지 수 (None이면 전체)
               테스트 시 max_pages=3 등으로 제한 가능
    """
    print("=" * 60)
    print("CamHR 크롤러 시작")
    print("=" * 60)

    # 1) 첫 페이지로 전체 규모 파악
    print("\n[1] 목록 API 호출 중...")
    first_response = fetch_job_list(page=1)
    data = first_response.get("data", {})
    total_count = int(data.get("totalCount", 0))
    total_pages = data.get("totalPage", 0)
    print(f"    전체 공고: {total_count}건, {total_pages}페이지")

    if max_pages:
        total_pages = min(total_pages, max_pages)
        print(f"    (max_pages={max_pages}로 제한)")

    # 2) 모든 페이지 순회
    all_posts = []

    for page in range(1, total_pages + 1):
        print(f"\n[2] {page}/{total_pages} 페이지 수집 중...")

        if page == 1:
            response_data = data
        else:
            response = fetch_job_list(page=page)
            response_data = response.get("data", {})
            time.sleep(0.5)  # API이므로 대기 시간 짧게

        results = response_data.get("result", [])
        print(f"    {len(results)}건 발견")

        for item in results:
            post = parse_list_item(item)
            all_posts.append(post)

    print(f"\n    총 {len(all_posts)}건 목록 수집 완료")

    # 3) 상세 페이지 크롤링 (처음 10건만 — 전체는 시간이 오래 걸림)
    detail_limit = min(10, len(all_posts))
    print(f"\n[3] 상세 정보 수집 중 (상위 {detail_limit}건)...")

    for i in range(detail_limit):
        post = all_posts[i]
        print(f"    ({i+1}/{detail_limit}) {post['title'][:50]}...")

        try:
            detail_response = fetch_job_detail(post["id"])
            detail_data = parse_detail(detail_response)
            post.update(detail_data)
        except Exception as e:
            print(f"    [ERROR] 상세 정보 실패: {e}")

        post["crawled_at"] = datetime.now().isoformat()
        time.sleep(0.5)

    # 나머지는 상세 없이 crawled_at만 추가
    for post in all_posts[detail_limit:]:
        post["crawled_at"] = datetime.now().isoformat()

    # 4) 저장
    data_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data")
    os.makedirs(data_dir, exist_ok=True)

    output_path = os.path.join(data_dir, "camhr_jobs.json")
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_posts, f, ensure_ascii=False, indent=2)

    print(f"\n[4] 저장 완료: {output_path}")
    print(f"    총 {len(all_posts)}건 (상세 정보: {detail_limit}건)")
    print("=" * 60)

    return all_posts


if __name__ == "__main__":
    # 테스트: 3페이지만 (45건)
    crawl_all(max_pages=3)
