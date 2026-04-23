"""
배치 analyze runner — data/site_candidates.json 의 후보를 순차 analyze.

URL 리스트는 `data/site_candidates.json` 에서 읽음 (카테고리별 정리).
analyzer 를 직접 import 하므로 main.py cmd_analyze 와 동일 결과.

실행:
    python scripts/batch_analyze.py                       # 등록 안 된 전체
    python scripts/batch_analyze.py --category 공공_지자체  # 카테고리 필터
    python scripts/batch_analyze.py --limit 10            # 앞 10개만
    python scripts/batch_analyze.py --include-registered  # 이미 등록된 것도 포함
    python scripts/batch_analyze.py --include-skipped     # skip=true 항목도 포함

결과: logs/batch_analyze_YYYYMMDD_HHMMSS.json + .txt
"""
import argparse
import sys
import io
import json
import time
import traceback
from pathlib import Path
from datetime import datetime

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# main.py 와 동일하게 ROOT + crawlers/ 를 path 에 추가
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawlers"))

CANDIDATES_PATH = ROOT / "data" / "site_candidates.json"


def load_candidates(
    *,
    category: str | None = None,
    limit: int | None = None,
    include_registered: bool = False,
    include_skipped: bool = False,
) -> list:
    """후보 JSON 을 필터링 옵션과 함께 로드.

    반환 포맷은 기존 CANDIDATES 와 동일한 (label, url) 튜플 리스트로 변환.
    label 은 site_id 가 있으면 그것, 없으면 name 을 슬러그화.
    """
    with open(CANDIDATES_PATH, encoding="utf-8") as f:
        entries = json.load(f)

    out = []
    for e in entries:
        if not include_registered and e.get("registered"):
            continue
        if not include_skipped and e.get("skip"):
            continue
        if category and e.get("category") != category:
            continue
        label = e.get("site_id") or e["name"]
        out.append((label, e["url"]))

    if limit is not None:
        out = out[:limit]
    return out


def _parse_args():
    p = argparse.ArgumentParser(description="배치 analyze — site_candidates.json 기반")
    p.add_argument("--category", help="특정 카테고리만 (예: 종합_포털, 공공_지자체, 직군_특화, 프리랜서, 대기업_채용, 커뮤니티, 교민)")
    p.add_argument("--limit", type=int, help="앞에서 N개만 처리")
    p.add_argument("--include-registered", action="store_true", help="이미 등록된 사이트도 포함")
    p.add_argument("--include-skipped", action="store_true", help="skip=true 표시된 항목도 포함")
    return p.parse_args()


def main():
    args = _parse_args()
    candidates = load_candidates(
        category=args.category,
        limit=args.limit,
        include_registered=args.include_registered,
        include_skipped=args.include_skipped,
    )
    if not candidates:
        print("처리할 후보 없음 — 필터 조건을 확인하거나 --include-registered 사용.")
        return

    # main.py 의 설정 그대로 임포트
    import main as app
    from analyzer import SiteAnalyzer
    from analyzer.strategies import (
        HeuristicStrategy,
        LLMStrategy,
        PlaywrightDiscoveryStrategy,
        EmbeddedJSONStrategy,
    )

    analyzer = SiteAnalyzer(
        use_llm=app.USE_LLM,
        llm_api_key=app.LLM_API_KEY,
        llm_model=app.LLM_MODEL,
        min_confidence=app.MIN_CONFIDENCE,
        strategies=[
            HeuristicStrategy(
                enabled=True,
                timeout=app.HTTP_TIMEOUT,
                max_retries=app.HTTP_MAX_RETRIES,
                retry_backoff=app.HTTP_RETRY_BACKOFF,
            ),
            PlaywrightDiscoveryStrategy(
                enabled=app.USE_PLAYWRIGHT_DISCOVERY,
                use_llm_ranker=app.USE_LLM_API_RANKER and app.USE_LLM,
                llm_api_key=app.LLM_API_KEY,
                llm_model=app.LLM_MODEL,
            ),
            EmbeddedJSONStrategy(
                enabled=True,
                timeout=app.HTTP_TIMEOUT,
                max_retries=app.HTTP_MAX_RETRIES,
                retry_backoff=app.HTTP_RETRY_BACKOFF,
                use_llm=app.USE_LLM,
                llm_api_key=app.LLM_API_KEY,
                llm_model=app.LLM_MODEL,
                use_playwright_render=app.USE_PLAYWRIGHT_RENDER,
            ),
            LLMStrategy(
                enabled=app.USE_LLM,
                api_key=app.LLM_API_KEY,
                model=app.LLM_MODEL,
            ),
        ],
    )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    json_path = logs_dir / f"batch_analyze_{timestamp}.json"
    txt_path = logs_dir / f"batch_analyze_{timestamp}.txt"

    print(f"=== 배치 analyze 시작: {len(candidates)} 개 ===")
    print(f"JSON → {json_path}")
    print(f"TXT  → {txt_path}")
    print()

    results = []

    with open(txt_path, "w", encoding="utf-8") as txt_f:
        for idx, (label, url) in enumerate(candidates, 1):
            print(f"[{idx}/{len(candidates)}] {label} — {url}")
            txt_f.write(f"\n{'='*70}\n[{idx}/{len(candidates)}] {label} — {url}\n{'='*70}\n")
            txt_f.flush()

            start = time.time()
            entry = {"label": label, "url": url}

            try:
                r = analyzer.analyze(url)
                elapsed = time.time() - start
                entry.update({
                    "ok": True,
                    "elapsed_sec": round(elapsed, 1),
                    "site_type": r.site_type.value,
                    "confidence": round(r.confidence, 2),
                    "strategy": r.strategy_name,
                    "is_valid": r.is_valid(app.MIN_CONFIDENCE),
                    "notes": r.notes,
                    "config": r.config,
                })
                print(f"  ✓ {r.site_type.value} conf={r.confidence:.2f} strategy={r.strategy_name} valid={r.is_valid(app.MIN_CONFIDENCE)} ({elapsed:.1f}s)")
                txt_f.write(f"OK: site_type={r.site_type.value} conf={r.confidence:.2f} strategy={r.strategy_name}\n")
                txt_f.write(f"is_valid={r.is_valid(app.MIN_CONFIDENCE)}\n")
                if r.notes:
                    txt_f.write(f"notes: {r.notes}\n")
                txt_f.write(f"config: {json.dumps(r.config, ensure_ascii=False, indent=2)}\n")
            except Exception as e:
                elapsed = time.time() - start
                entry.update({
                    "ok": False,
                    "elapsed_sec": round(elapsed, 1),
                    "error": f"{type(e).__name__}: {e}",
                    "traceback": traceback.format_exc(),
                })
                print(f"  ✗ ERROR: {type(e).__name__}: {e} ({elapsed:.1f}s)")
                txt_f.write(f"ERROR: {type(e).__name__}: {e}\n")
                txt_f.write(traceback.format_exc())

            txt_f.flush()
            results.append(entry)

            # 중간 저장 (인터럽트 대비)
            with open(json_path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=2)

    # 요약
    print()
    print("=== 요약 ===")
    ok_list = [r for r in results if r["ok"]]
    err_list = [r for r in results if not r["ok"]]
    valid_list = [r for r in ok_list if r.get("is_valid")]
    print(f"전체: {len(results)}")
    print(f"분석 성공 (ok): {len(ok_list)}")
    print(f"등록 가능 (is_valid): {len(valid_list)}")
    print(f"에러: {len(err_list)}")

    print("\n-- 등록 가능 후보 --")
    for r in valid_list:
        print(f"  {r['label']:<15} {r['site_type']:<20} conf={r['confidence']} strategy={r['strategy']}")

    print("\n-- 등록 불가 (분석은 성공) --")
    for r in ok_list:
        if not r.get("is_valid"):
            print(f"  {r['label']:<15} {r['site_type']:<20} conf={r['confidence']} strategy={r['strategy']}")

    print("\n-- 에러 --")
    for r in err_list:
        print(f"  {r['label']:<15} {r['error']}")


if __name__ == "__main__":
    main()
