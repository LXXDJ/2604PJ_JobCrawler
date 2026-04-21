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

# -- Validator retry 루프 (Phase 2.7) --
# cmd_add 에서 validate_dom_config 가 거부한 경우, 실패 사유를 LLM 에 돌려보내
# selectors 를 고쳐 받는 횟수. 0 이면 retry 안 함 (기존 동작).
# 1~2 가 실용적 — 3회 이상은 보통 LLM 이 같은 답을 반복.
# retry 는 DOM 경로만 대상 (embedded_json / api 는 실패 원인이 다양해 범위 밖).
VALIDATOR_RETRY_MAX = 2

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
SLACK_ENABLED = True
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
SLACK_ONLY_ISSUES = True     # True: 문제(warn/error) 있을 때만 전송 / False: 매 run마다 전송
# 배치 런 종료 요약을 Slack 에 매 run 보낼지 (사이트별 신규/재확인/실패 표).
# False 여도 위 헬스체크 경고는 여전히 SLACK_ONLY_ISSUES 규칙대로 전송됨.
SLACK_CRAWL_SUMMARY = True

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
        validate_api_config,
    )

    try:
        new_config = sites_registry.analysis_to_new_schema_config(result, url)
    except Exception as e:
        print(f"\n[거부] 신 스키마 변환 실패: {type(e).__name__}: {e}")
        return

    method = new_config["extraction_method"]

    if method == "api":
        # API 경로는 HTML 불필요 — 엔드포인트 직접 호출
        report = validate_api_config(new_config)
    else:
        # dom / embedded_json 은 HTML 필요
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

    retry_history: list = []  # Phase 2.7: retry 시도 기록 → validation_report 에 첨부

    # --- Phase 2.7: DOM validator 실패 시 LLM retry 루프 ---
    if (
        not report.ok
        and method == "dom"
        and VALIDATOR_RETRY_MAX > 0
        and LLM_API_KEY
    ):
        from analyzer.strategies.llm import retry_dom_selectors

        for attempt in range(1, VALIDATOR_RETRY_MAX + 1):
            prev_selectors = dict(new_config.get("source", {}).get("selectors") or {})
            print(f"\n  [retry {attempt}/{VALIDATOR_RETRY_MAX}] LLM 에 selectors 수정 요청...")
            print(f"    실패 사유 : {report.reason}")

            fixed, reasoning = retry_dom_selectors(
                html=html,
                failed_selectors=prev_selectors,
                failure_reason=report.reason,
                api_key=LLM_API_KEY,
                model=LLM_MODEL,
            )
            if fixed is None:
                print(f"    [retry {attempt}] 포기: {reasoning}")
                retry_history.append({
                    "attempt": attempt,
                    "input_reason": report.reason,
                    "outcome": "llm_gave_up",
                    "detail": reasoning,
                })
                break

            print(f"    제안 selectors: {fixed}")
            if reasoning:
                print(f"    이유: {reasoning}")

            # config 에 반영 — result.config 도 함께 (build_entry 시 재변환되므로)
            new_config["source"]["selectors"] = fixed
            result.config["selectors"] = fixed

            report = validate_dom_config(html, new_config)
            retry_history.append({
                "attempt": attempt,
                "input_reason": retry_history[-1]["input_reason"] if retry_history else "(첫 시도 실패 사유)",
                "proposed_selectors": fixed,
                "llm_reasoning": reasoning,
                "outcome": "ok" if report.ok else "fail",
                "new_reason": report.reason if not report.ok else "",
            })

            print(f"    [retry {attempt}] validator: ok={report.ok}")
            if report.sample_titles:
                print(f"                    샘플 제목: {report.sample_titles}")
            if report.fields_matched:
                print(f"                    매칭: {report.fields_matched}")
            if report.ok:
                break

    # --- API validator 실패 시 LLM Ranker 재시도 루프 ---
    # 필터옵션/코드테이블로 판명된 후보를 exclude 하고 LLM 에 다른 후보 요청.
    # 풀은 playwright_discovery 가 result.config["_ranker_pool"] 에 남겨둠.
    # (링커리어·알바몬처럼 GraphQL 필터 API / 브랜드코드 API 를 1위로 오인식하는 케이스용)
    if (
        not report.ok
        and method == "api"
        and VALIDATOR_RETRY_MAX > 0
        and LLM_API_KEY
    ):
        pool = result.config.get("_ranker_pool") or []
        prev_idx = result.config.get("_ranker_selected_index")
        excluded_idxs: list = [prev_idx] if isinstance(prev_idx, int) and prev_idx >= 0 else []

        if not pool:
            print("  [retry] API pool 정보 없음 (playwright_discovery 경로 아님) — 재시도 스킵")
        else:
            from analyzer.strategies.playwright_discovery import (
                llm_rank_candidates,
                build_config_from_candidate,
            )

            for attempt in range(1, VALIDATOR_RETRY_MAX + 1):
                print(
                    f"\n  [retry {attempt}/{VALIDATOR_RETRY_MAX}] "
                    f"LLM Ranker 재호출 (exclude={excluded_idxs})..."
                )
                print(f"    실패 사유 : {report.reason}")

                new_idx, rr_reason = llm_rank_candidates(
                    pool,
                    api_key=LLM_API_KEY,
                    model=LLM_MODEL,
                    exclude=excluded_idxs,
                )

                if new_idx is None or new_idx == -1:
                    print(f"    [retry {attempt}] 포기: {rr_reason}")
                    retry_history.append({
                        "attempt": attempt,
                        "input_reason": report.reason,
                        "excluded_indexes": list(excluded_idxs),
                        "outcome": "llm_gave_up",
                        "detail": rr_reason,
                    })
                    break

                print(f"    제안 idx={new_idx} — {pool[new_idx]['url']}")
                if rr_reason:
                    print(f"    이유: {rr_reason}")

                # pool[new_idx] 를 base 로 config 재생성. _ranker_pool 은 다음 재시도에도 필요.
                new_candidate_config = build_config_from_candidate(pool[new_idx], url)
                new_candidate_config["_ranker_pool"] = pool
                new_candidate_config["_ranker_selected_index"] = new_idx
                new_candidate_config["all_candidates"] = result.config.get("all_candidates", [])
                new_candidate_config["llm_reason"] = rr_reason
                result.config = new_candidate_config

                try:
                    new_config = sites_registry.analysis_to_new_schema_config(result, url)
                except Exception as e:
                    print(f"    [retry {attempt}] 신 스키마 변환 실패: {type(e).__name__}: {e}")
                    retry_history.append({
                        "attempt": attempt,
                        "input_reason": report.reason,
                        "excluded_indexes": list(excluded_idxs),
                        "proposed_idx": new_idx,
                        "outcome": "schema_conversion_fail",
                        "detail": str(e),
                    })
                    break

                report = validate_api_config(new_config)
                retry_history.append({
                    "attempt": attempt,
                    "excluded_indexes": list(excluded_idxs),
                    "proposed_idx": new_idx,
                    "proposed_endpoint": pool[new_idx]["url"],
                    "llm_reasoning": rr_reason,
                    "outcome": "ok" if report.ok else "fail",
                    "new_reason": report.reason if not report.ok else "",
                })

                print(f"    [retry {attempt}] validator: ok={report.ok}")
                if report.sample_titles:
                    print(f"                    샘플 제목: {report.sample_titles}")
                if report.fields_matched:
                    print(f"                    매칭: {report.fields_matched}")
                if report.ok:
                    break

                excluded_idxs.append(new_idx)

    if not report.ok:
        print(f"\n[거부] validator 실패: {report.reason}")
        if retry_history:
            print(f"  → retry {len(retry_history)}회 시도 후에도 실패.")
        print("  → sites.json에 저장하지 않음. selectors 재확인 필요.")
        return

    # --- 저장 (validated=true 로 마킹) ---
    # 주의: build_entry 를 쓰지 않고 new_config 를 직접 사용.
    # 이유: validator 가 2단계 중첩 자동 탐지로 source["item_path"] 를 [*] 형태로 업그레이드한
    # 경우, build_entry 는 result.config 에서 재계산해서 덮어쓰므로 mutation 이 사라진다.
    # 여기 시점엔 이미 new_config 가 "검증 통과한 최종 형태" 이므로 그대로 저장.
    from datetime import datetime, timezone
    report_dict = report.to_dict()
    if retry_history:
        report_dict["retry_history"] = retry_history
    entry = {
        "site_id": site_id,
        "url": url,
        "site_type": result.site_type.value,
        "added_at": datetime.now(timezone.utc).isoformat(),
        **new_config,
        "validated": True,
        "validation_report": report_dict,
    }
    entries.append(entry)
    sites_registry.save_all(SITES_JSON_PATH, entries)

    print(f"\n[OK] 등록 완료: {SITES_JSON_PATH}")
    print(f"      이제 `python main.py crawl` 실행 시 함께 수집됨.")


def _collect_all_entries(enabled_only: bool = False):
    """REGISTERED_CRAWLS + sites.json 병합 (crawl / health 공통).

    enabled_only=True 면 enabled 플래그가 False 인 항목 제외 — crawl 자동화
    대상에서 빼고 싶은 사이트 (예: JS 렌더 필요한 미구현 사이트) 용.
    플래그 없으면 기본 enabled 로 간주.
    """
    import sites_registry
    dynamic = sites_registry.load_all(SITES_JSON_PATH)
    entries = list(REGISTERED_CRAWLS) + dynamic
    if enabled_only:
        entries = [e for e in entries if e.get("enabled", True)]
    return entries


def cmd_crawl():
    """
    등록된 사이트들을 순차 크롤링 (REGISTERED_CRAWLS + sites.json 병합).
    enabled=false 인 항목은 건너뛴다.

    콘솔 + logs/crawl-YYYYMMDD.log 에 동시에 기록한다.
    끝에 헬스체크 리포트를 자동으로 덧붙임 (스케줄러로 돌면 여기가 유일한 알림 수단).
    """
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

        all_entries = _collect_all_entries(enabled_only=True)
        disabled = [e["site_id"] for e in _collect_all_entries()
                    if not e.get("enabled", True)]

        print(f"크롤링 대상: {len(all_entries)}개"
              + (f"  (비활성 제외: {', '.join(disabled)})" if disabled else ""))

        site_ids = [e["site_id"] for e in all_entries]

        import dispatcher
        batch_db = JobDatabase(DB_PATH)
        batch_started_iso = started_at.isoformat()
        per_site_stats = []

        for entry in all_entries:
            site_id = entry["site_id"]
            print(f"\n>>> 크롤링 시작: {site_id}")

            dispatch_error = None
            try:
                dispatcher.dispatch(entry, db_path=DB_PATH, http_config=http_config)
            except Exception as e:
                # 한 사이트 실패가 전체 crawl 을 중단시키지 않게.
                # 스케줄러로 매일 돌 때 한 사이트가 죽어도 나머지는 돌아야 함.
                print(f"  [ERROR] {site_id} 크롤링 실패: {type(e).__name__}: {e}")
                dispatch_error = f"{type(e).__name__}: {e}"

            # 이번 배치 런 동안 이 사이트에 생긴 crawl_runs 최신 row 조회.
            # dispatcher 가 crawl_runs row 를 만들기 전에 터지면 없을 수 있음 → dispatch_error 로 표기.
            run_row = None
            try:
                with batch_db.connect() as conn:
                    row = conn.execute(
                        "SELECT new_count, updated_count, error FROM crawl_runs "
                        "WHERE source = ? AND started_at >= ? "
                        "ORDER BY started_at DESC LIMIT 1",
                        (site_id, batch_started_iso),
                    ).fetchone()
                    if row is not None:
                        run_row = dict(row)
            except Exception as e:
                print(f"  [WARN] crawl_runs 조회 실패 ({site_id}): {type(e).__name__}: {e}")

            if run_row is not None:
                err = run_row.get("error") or dispatch_error
                per_site_stats.append({
                    "site_id": site_id,
                    "status": "error" if err else "ok",
                    "new_count": run_row.get("new_count") or 0,
                    "updated_count": run_row.get("updated_count") or 0,
                    "error": err,
                })
            else:
                per_site_stats.append({
                    "site_id": site_id,
                    "status": "error" if dispatch_error else "ok",
                    "new_count": 0,
                    "updated_count": 0,
                    "error": dispatch_error or "crawl_runs row 없음 (dispatcher 진입 전 실패 가능)",
                })

        finished_at = datetime.datetime.now()
        elapsed = finished_at - started_at
        print(f"\n[{finished_at:%Y-%m-%d %H:%M:%S}] crawl 완료 (소요 {elapsed})")

        if SLACK_ENABLED and SLACK_CRAWL_SUMMARY:
            if not SLACK_WEBHOOK_URL:
                print("      [WARN] SLACK_ENABLED=True 이지만 SLACK_WEBHOOK_URL 없음 — 요약 전송 스킵")
            else:
                import slack_notifier
                ok = slack_notifier.send_crawl_summary(
                    SLACK_WEBHOOK_URL,
                    per_site_stats,
                    started_at=started_at.strftime("%Y-%m-%d %H:%M:%S"),
                    finished_at=finished_at.strftime("%H:%M:%S"),
                    elapsed=str(elapsed).split(".")[0],
                )
                if ok:
                    print("      [Slack] 배치 요약 전송됨")

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
