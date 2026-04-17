"""
CamHR 크롤러 (API 기반) — DB 저장 + 증분 수집

- 사이트: https://www.camhr.com/
- 방식: REST API 직접 호출
- API 엔드포인트: https://api.camhr.com/v1.0.0/jobs/simple/page-query
- 상세 API: https://api.camhr.com/v1.0.0/jobs/{id}

증분 수집:
  - 매일 실행 시 목록 API로 전체 공고를 조회
  - 기존 DB에 있는 공고는 last_seen_at만 갱신
  - 신규 공고는 INSERT (상세 정보도 함께 수집)
  - 이렇게 하면 상세 API 호출이 최소화되어 빠르고 예의 바름
"""

import requests
import time
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")

from database import JobDatabase


API_BASE = "https://api.camhr.com/v1.0.0"
LIST_API = f"{API_BASE}/jobs/simple/page-query"
DETAIL_API = f"{API_BASE}/jobs"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Referer": "https://www.camhr.com/",
    "Accept": "application/json",
}

PAGE_SIZE = 50  # 한 페이지당 공고 수 (API 기본값보다 크게 — 전체 페이지 수 감소)

SOURCE_NAME = "camhr"


# ============================================================
# API 호출
# ============================================================

def fetch_job_list(page=1, size=PAGE_SIZE):
    """채용공고 목록 API 호출"""
    response = requests.get(
        LIST_API,
        params={"page": page, "size": size},
        headers=HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def fetch_job_detail(job_id):
    """채용공고 상세 API 호출"""
    response = requests.get(
        f"{DETAIL_API}/{job_id}",
        headers=HEADERS,
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


# ============================================================
# 데이터 정제 (DB 스키마에 맞게 변환)
# ============================================================

def item_to_job(item, detail_data=None):
    """
    API 응답 항목을 DB 스키마 형식으로 변환한다.

    item: 목록 API의 각 result 항목
    detail_data: 상세 API 응답의 data 부분 (신규 공고에만 필요)
    """
    employer = item.get("employer", {})
    salary = item.get("salaryId", {})
    term = item.get("termId", {})

    job = {
        "source": SOURCE_NAME,
        "external_id": item.get("id", ""),
        "title": item.get("title", ""),
        "company": employer.get("company", ""),
        "location": item.get("cities", ""),
        "salary": salary.get("label", "").strip(),
        "job_type": term.get("label", ""),
        "pub_date": item.get("pubdate", ""),
        "link": f"https://www.camhr.com/a/job/{item.get('id', '')}",
        "content": "",
        "raw_data": item,  # 목록 API의 원본 데이터 전부 보관
    }

    # 상세 정보가 있으면 content 필드에 채움
    if detail_data:
        requirement = detail_data.get("requirement", "") or ""
        description = detail_data.get("description", "") or ""
        job["content"] = (description + "\n\n[요구사항]\n" + requirement).strip()
        # 상세 정보도 raw_data에 포함
        job["raw_data"] = {
            "list": item,
            "detail": detail_data,
        }

    return job


# ============================================================
# 크롤링 실행
# ============================================================

def crawl(db_path="data/jobs.db", max_pages=None, fetch_detail_for_new=True):
    """
    CamHR 크롤링 + DB 저장.

    max_pages: 최대 페이지 수 (None이면 전체)
    fetch_detail_for_new: True면 신규 공고에 한해 상세 API도 호출
    """
    print("=" * 60)
    print("CamHR 크롤러 시작 (DB 저장 모드)")
    print("=" * 60)

    # DB 초기화
    db = JobDatabase(db_path)
    db.init_schema()

    # 크롤링 실행 로그 시작
    run_id = db.start_crawl_run(SOURCE_NAME)

    new_count = 0
    updated_count = 0
    error_msg = None

    try:
        # 1) 첫 페이지 → 전체 규모 파악
        print("\n[1] 목록 API 호출 중...")
        first_response = fetch_job_list(page=1)
        first_data = first_response.get("data", {})
        total_count = int(first_data.get("totalCount", 0))
        total_pages = first_data.get("totalPage", 0)
        print(f"    전체 공고: {total_count}건, {total_pages}페이지 (size={PAGE_SIZE})")

        if max_pages:
            total_pages = min(total_pages, max_pages)
            print(f"    (max_pages={max_pages}로 제한)")

        # 2) 모든 페이지 순회
        for page in range(1, total_pages + 1):
            if page == 1:
                response_data = first_data
            else:
                response = fetch_job_list(page=page)
                response_data = response.get("data", {})
                time.sleep(0.3)

            results = response_data.get("result", [])
            print(f"\n[2] {page}/{total_pages} 페이지 — {len(results)}건 처리 중...")

            for item in results:
                job_id = item.get("id", "")

                # 이미 DB에 있는지 체크 — 빠른 확인용
                existing = _check_exists(db, SOURCE_NAME, job_id)

                if existing:
                    # 기존 공고 — last_seen_at만 갱신
                    job = item_to_job(item)
                    db.upsert_job(job)
                    updated_count += 1
                else:
                    # 신규 공고 — 상세 API도 호출
                    detail_data = None
                    if fetch_detail_for_new:
                        try:
                            detail_response = fetch_job_detail(job_id)
                            detail_data = detail_response.get("data", {})
                            time.sleep(0.3)
                        except Exception as e:
                            print(f"      [WARN] 상세 정보 실패 ({job_id}): {e}")

                    job = item_to_job(item, detail_data)
                    db.upsert_job(job)
                    new_count += 1
                    print(f"      [NEW] {job['title'][:50]}")

            print(f"    누적: 신규 {new_count}, 기존 {updated_count}")

        db.finish_crawl_run(run_id, new_count, updated_count)

    except Exception as e:
        error_msg = str(e)
        db.finish_crawl_run(run_id, new_count, updated_count, error=error_msg)
        raise

    # 결과 요약
    print(f"\n{'='*60}")
    print(f"CamHR 크롤링 완료")
    print(f"  신규: {new_count}건 / 기존 재확인: {updated_count}건")
    print(f"  DB: {db_path}")
    print(f"{'='*60}")

    # 통계 출력
    stats = db.get_stats()
    print("\n[전체 DB 현황]")
    for s in stats:
        print(f"  [{s['source']}] {s['count']}건 "
              f"(첫 수집: {s['first_seen'][:10]}, 최신: {s['last_seen'][:10]})")

    return {"new": new_count, "updated": updated_count}


def _check_exists(db, source, external_id):
    """DB에 이미 있는지 빠르게 확인"""
    with db.connect() as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE source = ? AND external_id = ? LIMIT 1",
            (source, external_id)
        ).fetchone()
        return row is not None


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-pages", type=int, default=None,
                        help="최대 페이지 수 (테스트용, 미지정 시 전체)")
    parser.add_argument("--db", type=str, default=None,
                        help="DB 파일 경로 (기본: data/jobs.db)")
    args = parser.parse_args()

    # 프로젝트 루트 기준 경로
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = args.db or os.path.join(project_root, "data", "jobs.db")

    crawl(db_path=db_path, max_pages=args.max_pages)
