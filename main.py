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
import datetime

from dotenv import load_dotenv

if sys.stdout is not None:
    sys.stdout.reconfigure(encoding="utf-8")


class _Tee:
    """
    여러 스트림에 동시에 쓰는 file-like 객체.
    cmd_crawl에서 콘솔 + 로그 파일에 같은 내용을 출력하기 위해 사용.
    pythonw 등으로 콘솔이 없을 때 원본 stdout이 None이어도 안전하게 동작하도록 필터링.
    """
    def __init__(self, *streams):
        self.streams = [s for s in streams if s is not None]

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
                s.flush()
            except Exception:
                pass

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:
                pass

# 프로젝트 내부 import 경로
ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "crawlers"))

# .env 자동 로드 (프로젝트 루트의 .env 파일에서 환경변수 읽음).
# 파일 없으면 조용히 넘어감 — OS 환경변수만 쓰는 경우도 허용.
load_dotenv(os.path.join(ROOT, ".env"))

# ============================================================
# [SETTINGS] — 이 섹션만 수정하면 동작이 바뀜
# ============================================================

# -- LLM 분석 전략 사용 여부 --
# False: 휴리스틱만 사용 (빠르고 무료, 알려진 패턴만 커버)
# True : 휴리스틱 실패 시 LLM(OpenAI) 폴백 (느리지만 범용, API 비용)
USE_LLM = True

# LLM 설정 (USE_LLM=True일 때만 사용)
LLM_API_KEY = os.getenv("OPENAI_API_KEY")  # 환경변수에서 읽음
LLM_MODEL = "gpt-4o-mini"

# -- Playwright API 자동 발견 --
# True : heuristic 이 SPA (Nuxt/Next/Vue/React) 로 판정한 경우에 한해
#        헤드리스 Chromium 을 띄워 내부 API 엔드포인트 후보를 자동으로 스니핑.
#        결과는 "수동 어댑터 작성용 재료" — 자동 크롤링은 안 함.
# False: SPA 는 그대로 거부 (사람이 개발자도구로 API 찾아서 hardcoded_crawls.py 에 등록)
USE_PLAYWRIGHT_DISCOVERY = True

# -- Playwright 렌더 경로 (embedded JSON Phase 2.5) --
# True : HTML 에 #__NEXT_DATA__ 가 없는 경우, 페이지를 실제 렌더해서
#        window.__NUXT__ / window.__NEXT_DATA__ 같은 전역 변수에서 state 를 뽑는다.
#        → JobKorea 같은 __NUXT__ 팩토리 / CSR 사이트 대응.
#        등록된 사이트는 크롤링 시에도 매 페이지 렌더 필요 (비용 ↑).
# False: HTML-only 만 시도 (빠르지만 factory form 은 실패)
USE_PLAYWRIGHT_RENDER = True

# -- Playwright API 후보 LLM 랭커 --
# True : 규칙 점수화 상위 10개를 LLM 에게 넘겨 "진짜 공고 리스트 API" 를 재선별.
#        메타데이터/필터옵션 API 를 걸러내는 데 효과적.
#        USE_LLM=True 이고 LLM_API_KEY 있을 때만 실제 호출. 실패 시 규칙점수 1위로 폴백.
# False: 규칙 점수 1위 그대로 사용 (기존 동작)
USE_LLM_API_RANKER = True

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

# -- 동적 등록 사이트 저장소 --
# `python main.py add <URL>` 로 등록된 사이트들이 여기 쌓임.
# REGISTERED_CRAWLS(아래 import) 는 analyzer 로 자동 등록 불가능한 특수 사이트용.
SITES_JSON_PATH = os.path.join(ROOT, "data", "sites.json")

# -- 로그 디렉토리 --
# `crawl` 명령이 실행될 때마다 logs/crawl-YYYYMMDD.log 에 append된다.
# Windows 작업 스케줄러로 백그라운드 실행할 때 실행 이력/에러 추적용.
LOG_DIR = os.path.join(ROOT, "logs")

# -- Slack 알림 --
# cmd_crawl 끝의 헬스체크 결과를 Slack webhook으로 전송.
# 실제 알림 받기 시작할 준비가 되면 SLACK_ENABLED=True 로 바꾸고 환경변수 세팅.
#   1) Slack에서 Incoming Webhook 활성화 → webhook URL 발급
#      (https://api.slack.com/messaging/webhooks)
#   2) 환경변수로 등록:
#      Windows: setx SLACK_WEBHOOK_URL "https://hooks.slack.com/services/..."
#      bash   : export SLACK_WEBHOOK_URL="https://hooks.slack.com/services/..."
SLACK_ENABLED = False
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_ONLY_ISSUES = True     # True: 문제(warn/error) 있을 때만 전송 / False: 매 run마다 전송

# -- 등록된 크롤링 대상 (특수 사이트 전용) --
# analyzer 로 자동 등록이 불가능한 사이트만 여기에 하드코딩.
# 일반 gnuboard 같은 사이트는 `python main.py add <URL>` 로 등록 → data/sites.json 에 쌓임.
# 상세: crawlers/hardcoded_crawls.py
from hardcoded_crawls import REGISTERED_CRAWLS


# ============================================================
# 명령 처리
# ============================================================

def cmd_analyze(url: str):
    """사이트 분석 명령 — URL을 받아서 config 생성"""
    from analyzer import SiteAnalyzer
    from analyzer.strategies import (
        HeuristicStrategy,
        LLMStrategy,
        PlaywrightDiscoveryStrategy,
        EmbeddedJSONStrategy,
    )

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
            PlaywrightDiscoveryStrategy(
                enabled=USE_PLAYWRIGHT_DISCOVERY,
                use_llm_ranker=USE_LLM_API_RANKER and USE_LLM,
                llm_api_key=LLM_API_KEY,
                llm_model=LLM_MODEL,
            ),
            EmbeddedJSONStrategy(
                enabled=True,
                timeout=HTTP_TIMEOUT,
                max_retries=HTTP_MAX_RETRIES,
                retry_backoff=HTTP_RETRY_BACKOFF,
                use_llm=USE_LLM,
                llm_api_key=LLM_API_KEY,
                llm_model=LLM_MODEL,
                use_playwright_render=USE_PLAYWRIGHT_RENDER,
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


def cmd_add(url: str):
    """
    사이트 분석 후 자동 등록 (sites.json에 저장).

    흐름: analyze → 등록 가능성 체크 → 중복 확인 → 저장
    등록 거부되는 경우는 sites_registry.can_register 참조.
    """
    from analyzer import SiteAnalyzer
    from analyzer.strategies import (
        HeuristicStrategy,
        LLMStrategy,
        PlaywrightDiscoveryStrategy,
        EmbeddedJSONStrategy,
    )
    import sites_registry

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
            PlaywrightDiscoveryStrategy(
                enabled=USE_PLAYWRIGHT_DISCOVERY,
                use_llm_ranker=USE_LLM_API_RANKER and USE_LLM,
                llm_api_key=LLM_API_KEY,
                llm_model=LLM_MODEL,
            ),
            EmbeddedJSONStrategy(
                enabled=True,
                timeout=HTTP_TIMEOUT,
                max_retries=HTTP_MAX_RETRIES,
                retry_backoff=HTTP_RETRY_BACKOFF,
                use_llm=USE_LLM,
                llm_api_key=LLM_API_KEY,
                llm_model=LLM_MODEL,
                use_playwright_render=USE_PLAYWRIGHT_RENDER,
            ),
            LLMStrategy(enabled=USE_LLM, api_key=LLM_API_KEY, model=LLM_MODEL),
        ],
    )

    print(f"분석 시작: {url}\n")
    result = analyzer.analyze(url)

    print(f"  site_type  : {result.site_type.value}")
    print(f"  confidence : {result.confidence:.2f}")
    print(f"  strategy   : {result.strategy_name}")
    if result.notes:
        print(f"  notes      : {result.notes}")

    # --- 등록 가능성 체크 (1차 구조) ---
    ok, reason = sites_registry.can_register(result)
    if not ok:
        print(f"\n[거부] {reason}")
        print("  → sites.json에 등록하지 않음.")
        return

    # --- 중복 체크용: result.config 를 entry 모양으로 감싸기 ---
    candidate_entry = sites_registry.wrap_flat_config_as_entry(result.config)

    # --- 하드코딩된 REGISTERED_CRAWLS와의 중복 체크 ---
    hardcoded_dup = sites_registry.find_duplicate(candidate_entry, REGISTERED_CRAWLS)
    if hardcoded_dup:
        print(f"\n[경고] 이 사이트/보드는 REGISTERED_CRAWLS에 이미 등록돼 있음 "
              f"(site_id='{hardcoded_dup['site_id']}').")
        print(f"       sites.json에도 추가하면 매 crawl 실행 시 중복 크롤링됨.")
        if input("그래도 진행? (y/N): ").strip().lower() != "y":
            print("취소.")
            return

    # --- sites.json 내 중복 체크 ---
    entries = sites_registry.load_all(SITES_JSON_PATH)
    existing = sites_registry.find_duplicate(candidate_entry, entries)

    if existing:
        print(f"\n이미 등록된 사이트:")
        print(f"  site_id    : {existing['site_id']}")
        print(f"  url        : {existing['url']}")
        print(f"  added_at   : {existing['added_at']}")
        choice = input("덮어쓸까요? (y=덮어쓰기 / 그 외=스킵): ").strip().lower()
        if choice != "y":
            print("스킵.")
            return
        # 기존 엔트리 제거하고 site_id 재사용
        entries = [e for e in entries if e.get("site_id") != existing["site_id"]]
        site_id = existing["site_id"]
    else:
        # --- site_id 자동 추출 + 충돌 회피 ---
        desired = sites_registry.extract_site_id(url)
        taken = {e["site_id"] for e in REGISTERED_CRAWLS}
        taken.update(e["site_id"] for e in entries)
        site_id = sites_registry.resolve_unique_site_id(desired, taken)
        if site_id != desired:
            print(f"\n[정보] site_id '{desired}'가 이미 사용중 → '{site_id}'로 등록")

    print(f"\n  site_id    : {site_id}")

    # --- 2차 검증: validator 로 실제 HTML/state 에서 config 동작 확인 ---
    # analyzer 가 받은 HTML 을 노출하지 않으므로 한 번 더 fetch.
    # LLM 이 환각한 selectors/경로나 stale 테마를 여기서 잡는다.
    from http_client import fetch
    from analyzer.validator import (
        validate_dom_config,
        validate_embedded_json_config,
    )

    try:
        html = fetch(
            url,
            timeout=HTTP_TIMEOUT,
            max_retries=HTTP_MAX_RETRIES,
            retry_backoff=HTTP_RETRY_BACKOFF,
        )
    except Exception as e:
        print(f"\n[거부] 검증용 HTML 재다운로드 실패: {type(e).__name__}: {e}")
        return

    try:
        new_config = sites_registry.analysis_to_new_schema_config(result, url)
    except Exception as e:
        print(f"\n[거부] 신 스키마 변환 실패: {type(e).__name__}: {e}")
        return

    method = new_config["extraction_method"]
    if method == "dom":
        report = validate_dom_config(html, new_config)
    elif method == "embedded_json":
        # 렌더 경로는 url 필요 (html 은 무시됨)
        report = validate_embedded_json_config(html, new_config, url=url)
    else:
        print(f"\n[거부] extraction_method={method!r} 에 대한 validator 없음")
        return
    print(f"\n  validator  : ok={report.ok}")
    if report.sample_titles:
        print(f"               샘플 제목: {report.sample_titles}")
    if report.fields_matched:
        print(f"               매칭: {report.fields_matched}")

    if not report.ok:
        print(f"\n[거부] validator 실패: {report.reason}")
        print("  → sites.json에 저장하지 않음. selectors 재확인 필요.")
        return

    # --- 저장 (validated=true 로 마킹) ---
    entries.append(sites_registry.build_entry(result, site_id, validation_report=report.to_dict()))
    sites_registry.save_all(SITES_JSON_PATH, entries)

    print(f"\n[OK] 등록 완료: {SITES_JSON_PATH}")
    print(f"      이제 `python main.py crawl` 실행 시 함께 수집됨.")


def _collect_all_entries():
    """REGISTERED_CRAWLS + sites.json 병합 (crawl / health 공통)"""
    import sites_registry
    dynamic = sites_registry.load_all(SITES_JSON_PATH)
    return list(REGISTERED_CRAWLS) + dynamic


def cmd_crawl():
    """
    등록된 사이트들을 순차 크롤링 (REGISTERED_CRAWLS + sites.json 병합).

    콘솔 + logs/crawl-YYYYMMDD.log 에 동시에 기록한다.
    끝에 헬스체크 리포트를 자동으로 덧붙임 (스케줄러로 돌면 여기가 유일한 알림 수단).
    """
    import sites_registry
    from database import JobDatabase
    import healthcheck

    os.makedirs(LOG_DIR, exist_ok=True)
    started_at = datetime.datetime.now()
    log_path = os.path.join(LOG_DIR, f"crawl-{started_at:%Y%m%d}.log")
    log_file = open(log_path, "a", encoding="utf-8")
    original_stdout = sys.stdout
    sys.stdout = _Tee(original_stdout, log_file)

    try:
        print(f"\n{'=' * 60}")
        print(f"[{started_at:%Y-%m-%d %H:%M:%S}] crawl 시작")
        print(f"{'=' * 60}")

        http_config = {
            "timeout": HTTP_TIMEOUT,
            "max_retries": HTTP_MAX_RETRIES,
            "retry_backoff": HTTP_RETRY_BACKOFF,
        }

        dynamic_entries = sites_registry.load_all(SITES_JSON_PATH)
        all_entries = list(REGISTERED_CRAWLS) + dynamic_entries

        print(f"크롤링 대상: 기본 {len(REGISTERED_CRAWLS)}개 + 동적 {len(dynamic_entries)}개 "
              f"= 총 {len(all_entries)}개")

        site_ids = [e["site_id"] for e in all_entries]

        import dispatcher

        for entry in all_entries:
            site_id = entry["site_id"]
            print(f"\n>>> 크롤링 시작: {site_id}")

            try:
                dispatcher.dispatch(entry, db_path=DB_PATH, http_config=http_config)
            except Exception as e:
                # 한 사이트 실패가 전체 crawl 을 중단시키지 않게.
                # 스케줄러로 매일 돌 때 한 사이트가 죽어도 나머지는 돌아야 함.
                print(f"  [ERROR] {site_id} 크롤링 실패: {type(e).__name__}: {e}")

        finished_at = datetime.datetime.now()
        elapsed = finished_at - started_at
        print(f"\n[{finished_at:%Y-%m-%d %H:%M:%S}] crawl 완료 (소요 {elapsed})")

        # 자동 헬스체크 리포트 — 스케줄러로 돌 때 문제를 놓치지 않으려면 필수
        try:
            db = JobDatabase(DB_PATH)
            report = healthcheck.analyze(db, site_ids)
            print()
            # show_ok=False: 자동 리포트는 문제만 간결하게
            print(healthcheck.format_text(report, show_ok=False))

            # Slack 알림 (설정에서 꺼져있으면 스킵)
            if SLACK_ENABLED:
                if not SLACK_WEBHOOK_URL:
                    print("      [WARN] SLACK_ENABLED=True 이지만 "
                          "SLACK_WEBHOOK_URL 환경변수가 없음 — 전송 스킵")
                else:
                    import slack_notifier
                    sent = slack_notifier.send(
                        SLACK_WEBHOOK_URL, report,
                        only_issues=SLACK_ONLY_ISSUES,
                    )
                    if sent:
                        print("      [Slack] 알림 전송됨")
                    elif SLACK_ONLY_ISSUES and not report.has_issues:
                        print("      [Slack] 정상 상태 — 전송 스킵 (SLACK_ONLY_ISSUES=True)")

        except Exception as e:
            print(f"\n[WARN] 헬스체크 실행 실패: {type(e).__name__}: {e}")

    finally:
        sys.stdout = original_stdout
        log_file.close()


def cmd_health():
    """수동 헬스체크 — 등록된 모든 사이트의 상태 리포트 출력"""
    from database import JobDatabase
    import healthcheck

    all_entries = _collect_all_entries()
    site_ids = [e["site_id"] for e in all_entries]

    db = JobDatabase(DB_PATH)
    report = healthcheck.analyze(db, site_ids)
    print(healthcheck.format_text(report, show_ok=True))


def cmd_notify_test():
    """
    Slack webhook 연결 검증 — 가짜 HealthReport를 한 번 전송.

    crawl을 기다릴 필요 없이 SLACK_WEBHOOK_URL 세팅이 제대로 됐는지 바로 확인 가능.
    webhook URL 바꿀 때마다 재검증하기 좋음.
    """
    import slack_notifier
    from healthcheck import HealthReport, SiteHealth

    if not SLACK_WEBHOOK_URL:
        print("[ERROR] SLACK_WEBHOOK_URL 환경변수가 없음.")
        print("        .env 파일에 SLACK_WEBHOOK_URL=... 추가하거나 OS 환경변수로 등록.")
        sys.exit(1)

    report = HealthReport(
        generated_at=datetime.datetime.now().isoformat(timespec="seconds"),
        sites=[
            SiteHealth(
                site_id="notify-test-error",
                status="error",
                issues=["(테스트) 크롤러 예외 시뮬레이션", "최근 3회 중 3회 에러"],
                last_error="TestError: connection refused",
                recent_error_count=3,
            ),
            SiteHealth(
                site_id="notify-test-warn",
                status="warn",
                issues=["(테스트) 수집량 급감 시뮬레이션 (50건 < 평균 200의 50%)"],
                recent_total=50,
                avg_total_prior=200.0,
            ),
            SiteHealth(site_id="notify-test-ok", status="ok", issues=[]),
        ],
    )

    print("Slack 전송 시도 중 (only_issues=False — 테스트니까 무조건 전송)...")
    sent = slack_notifier.send(SLACK_WEBHOOK_URL, report, only_issues=False)
    if sent:
        print("[OK] 전송 성공 — Slack 채널에서 메시지 확인.")
    else:
        print("[FAIL] 전송 실패 — 위 [WARN] 로그 참조.")
        sys.exit(1)


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

    analyze_parser = subparsers.add_parser("analyze", help="새 사이트 분석 (결과 출력만)")
    analyze_parser.add_argument("url", help="분석할 사이트 URL")

    add_parser = subparsers.add_parser("add", help="사이트 분석 + sites.json에 자동 등록")
    add_parser.add_argument("url", help="등록할 사이트 URL")

    subparsers.add_parser("crawl", help="등록된 사이트 크롤링")
    subparsers.add_parser("stats", help="DB 통계")
    subparsers.add_parser("health", help="등록 사이트 건강 상태 점검 (에러/0건/급감/스테일)")
    subparsers.add_parser("notify-test", help="Slack webhook 연결 검증 — 더미 알림 1회 전송")

    args = parser.parse_args()

    if args.command == "analyze":
        cmd_analyze(args.url)
    elif args.command == "add":
        cmd_add(args.url)
    elif args.command == "crawl":
        cmd_crawl()
    elif args.command == "stats":
        cmd_stats()
    elif args.command == "health":
        cmd_health()
    elif args.command == "notify-test":
        cmd_notify_test()


if __name__ == "__main__":
    main()
