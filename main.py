"""
JobCrawler 엔트리포인트

이 파일 상단의 [SETTINGS] 섹션만 수정하면 동작이 바뀌도록 설계됨.
실제 로직은 crawlers/ 모듈에 있음.

실행:
  python main.py analyze <URL>    # 새 사이트 분석
  python main.py crawl             # 등록된 사이트 크롤링
  python main.py stats             # DB 통계
"""

import os
import sys
import json
import argparse

sys.stdout.reconfigure(encoding="utf-8")

# 프로젝트 내부 import 경로
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "crawlers"))

# ============================================================
# [SETTINGS] — 이 섹션만 수정하면 동작이 바뀜
# ============================================================

# -- LLM 분석 전략 사용 여부 --
# False: 휴리스틱만 사용 (빠르고 무료, 알려진 패턴만 커버)
# True : 휴리스틱 실패 시 LLM(Claude) 폴백 (느리지만 범용, API 비용)
USE_LLM = False

# LLM 설정 (USE_LLM=True일 때만 사용)
LLM_API_KEY = os.getenv("ANTHROPIC_API_KEY")  # 환경변수에서 읽음
LLM_MODEL = "claude-sonnet-4-6"

# -- 분석 결과 신뢰도 임계값 --
# 이 값보다 낮으면 유효하지 않다고 판단 (다음 전략 시도 or 실패)
MIN_CONFIDENCE = 0.5

# -- HTTP 요청 설정 (분석기 및 크롤러 공통) --
HTTP_TIMEOUT = 30            # 1회 요청 타임아웃(초)
HTTP_MAX_RETRIES = 3         # 실패 시 재시도 횟수
HTTP_RETRY_BACKOFF = 2.0     # 재시도 간 대기 배수 (1회 실패 시 N초, 2회 실패 시 N*2초 대기)
                             # 값이 클수록 느려지지만 불안정한 서버에 유리

# -- DB 경로 --
DB_PATH = os.path.join(ROOT, "data", "jobs.db")

# -- 크롤링 설정 --
# CamHR 최대 페이지 수 (None이면 전체 수집)
# 테스트 시 3~5 정도로 제한하면 빠름
CAMHR_MAX_PAGES = 3

# -- 등록된 크롤링 대상 --
# 각 사이트는 (site_id, crawler_func, kwargs) 형태
# 나중에 여기를 동적으로 — 분석기로 생성한 config에서 — 만들 수 있음
REGISTERED_CRAWLS = [
    ("camhr", "camhr_crawler", {}),  # camhr_crawler.crawl() 호출
]


# ============================================================
# 명령 처리
# ============================================================

def cmd_analyze(url: str):
    """사이트 분석 명령 — URL을 받아서 config 생성"""
    from analyzer import SiteAnalyzer
    from analyzer.strategies import HeuristicStrategy, LLMStrategy

    analyzer = SiteAnalyzer(
        use_llm=USE_LLM,
        llm_api_key=LLM_API_KEY,
        llm_model=LLM_MODEL,
        min_confidence=MIN_CONFIDENCE,
        strategies=[
            HeuristicStrategy(
                enabled=True,
                timeout=HTTP_TIMEOUT,
                max_retries=HTTP_MAX_RETRIES,
                retry_backoff=HTTP_RETRY_BACKOFF,
            ),
            LLMStrategy(
                enabled=USE_LLM,
                api_key=LLM_API_KEY,
                model=LLM_MODEL,
            ),
        ],
    )

    print(f"분석 시작: {url}")
    print(f"  USE_LLM = {USE_LLM}")
    print(f"  활성화된 전략: {[s.name for s in analyzer.strategies if s.enabled]}")
    print()

    result = analyzer.analyze(url)

    print("=" * 60)
    print("분석 결과")
    print("=" * 60)
    print(f"  URL          : {result.url}")
    print(f"  site_type    : {result.site_type.value}")
    print(f"  confidence   : {result.confidence:.2f}")
    print(f"  strategy     : {result.strategy_name}")
    print(f"  유효한가     : {result.is_valid(MIN_CONFIDENCE)}")
    if result.notes:
        print(f"  notes        : {result.notes}")

    print("\n  config:")
    print(json.dumps(result.config, ensure_ascii=False, indent=4))


def cmd_crawl():
    """등록된 사이트들을 순차 크롤링"""
    for site_id, module_name, kwargs in REGISTERED_CRAWLS:
        print(f"\n>>> 크롤링 시작: {site_id}")

        if site_id == "camhr":
            import camhr_crawler
            camhr_crawler.crawl(
                db_path=DB_PATH,
                max_pages=CAMHR_MAX_PAGES,
                **kwargs,
            )


def cmd_stats():
    """DB 통계 출력"""
    from database import JobDatabase

    db = JobDatabase(DB_PATH)
    stats = db.get_stats()

    print("=" * 60)
    print(f"DB: {DB_PATH}")
    print("=" * 60)

    if not stats:
        print("  (데이터 없음)")
        return

    for s in stats:
        print(f"  [{s['source']}] {s['count']}건")
        print(f"    최초 수집: {s['first_seen']}")
        print(f"    최신 확인: {s['last_seen']}")

    print("\n최근 크롤링 실행 이력:")
    for run in db.get_recent_runs(limit=5):
        status = "OK" if not run['error'] else f"ERROR: {run['error']}"
        print(f"  [{run['source']}] {run['started_at'][:19]} "
              f"→ 신규 {run['new_count']}, 재확인 {run['updated_count']} ({status})")


# ============================================================
# CLI
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="JobCrawler")
    subparsers = parser.add_subparsers(dest="command", required=True)

    analyze_parser = subparsers.add_parser("analyze", help="새 사이트 분석")
    analyze_parser.add_argument("url", help="분석할 사이트 URL")

    subparsers.add_parser("crawl", help="등록된 사이트 크롤링")
    subparsers.add_parser("stats", help="DB 통계")

    args = parser.parse_args()

    if args.command == "analyze":
        cmd_analyze(args.url)
    elif args.command == "crawl":
        cmd_crawl()
    elif args.command == "stats":
        cmd_stats()


if __name__ == "__main__":
    main()
