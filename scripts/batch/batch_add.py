"""
배치 add runner — data/site_candidates.json 의 후보를 순차적으로 등록.

batch_analyze 와 달리 **실제로 sites.json 에 저장** 까지 수행한다 (내부적으로
main.py 의 cmd_add 를 subprocess 로 호출). 각 add 에 Playwright + LLM 이
들어가면 30~90초 걸리므로 긴 작업 — `--limit` 로 끊어서 돌리는 걸 권장.

실행:
    python scripts/batch_add.py                       # 등록 안 된 전체
    python scripts/batch_add.py --category 종합_포털   # 카테고리만
    python scripts/batch_add.py --limit 5             # 앞 5개
    python scripts/batch_add.py --dry-run             # URL 도달성만 체크, 실제 add 안 함

로직:
    1. site_candidates.json 읽음
    2. registered=true, skip=true 필터링
    3. 기존 sites.json 에 이미 같은 URL 있으면 스킵 (중복 방어)
    4. preflight: HEAD/GET 으로 URL 도달성 확인 (404 는 바로 탈락)
    5. `python main.py add <URL>` 호출, stdin 에 "n\n" 피딩 (덮어쓰기 프롬프트 대비)
    6. 각 결과 파싱 — `[OK] 등록 완료` / `[거부]` 로 분기 판정
    7. logs/batch_add_YYYYMMDD_HHMMSS.json + .txt 에 전체 결과 저장

결과 상태:
    "registered" — 성공적으로 sites.json 에 추가
    "rejected"   — main.py add 가 validator 실패 등으로 거부
    "dup"        — sites.json 에 이미 같은 URL 존재
    "unreachable"— preflight 에서 404/연결 실패
    "skipped"    — JSON 에서 skip=true 또는 CLI 옵션으로 제외
    "error"      — subprocess 중 예외
"""
import argparse
import io
import json
import subprocess
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "crawlers"))

CANDIDATES_PATH = ROOT / "data" / "site_candidates.json"
SITES_JSON_PATH = ROOT / "data" / "sites.json"


def _parse_args():
    p = argparse.ArgumentParser(description="배치 add — site_candidates.json 기반 자동 등록")
    p.add_argument("--category", help="특정 카테고리만 (예: 종합_포털, 공공_지자체, 직군_특화, 프리랜서, 대기업_채용, 커뮤니티, 교민)")
    p.add_argument("--limit", type=int, help="앞에서 N개만 처리")
    p.add_argument("--dry-run", action="store_true", help="preflight(URL 도달성) 만, 실제 add 호출 안 함")
    p.add_argument("--timeout", type=int, default=180, help="사이트별 add 타임아웃 (초, 기본 180)")
    return p.parse_args()


def _load_candidates(category, limit):
    with open(CANDIDATES_PATH, encoding="utf-8") as f:
        entries = json.load(f)
    out = []
    for e in entries:
        if e.get("registered"):
            continue  # 이미 sites.json 에 있음
        if category and e.get("category") != category:
            continue
        out.append(e)
    if limit is not None:
        out = out[:limit]
    return out


def _existing_urls() -> set:
    """sites.json 에 이미 등록된 URL 집합 (중복 방어용)."""
    if not SITES_JSON_PATH.exists():
        return set()
    with open(SITES_JSON_PATH, encoding="utf-8") as f:
        entries = json.load(f)
    return {e.get("url") for e in entries if e.get("url")}


def _preflight(url: str, timeout: int = 10) -> tuple[bool, str]:
    """URL 도달성 체크 — 실제 연결 불가 케이스만 탈락시킨다.

    - DNS 실패 / 연결 타임아웃 / SSL 오류 → 탈락 (Playwright 도 못 뚫음)
    - 4xx/5xx → **통과** (analyzer 의 Playwright discovery 가 뚫을 수 있음.
      리멤버·자소설닷컴 같은 사이트는 기본 GET 에 403 이지만 Playwright 렌더로는 정상.)
    """
    try:
        from http_client import fetch
        fetch(url, timeout=timeout, max_retries=1)
        return True, ""
    except Exception as e:
        name = type(e).__name__
        # HTTP 에러는 preflight 단계에선 통과시키고 add 의 실제 analyzer 에 맡김
        if name in ("HTTPError",):
            return True, f"(preflight 에서 {name} 이나 통과 — Playwright 에 맡김)"
        return False, f"{name}: {str(e)[:150]}"


def _run_add(url: str, timeout: int) -> tuple[str, str]:
    """main.py add 를 subprocess 로 실행 — 결과 문자열과 판정.

    stdin 에 'n\n' 를 파이프해서 덮어쓰기 프롬프트가 떠도 스킵으로 답하게.

    반환: (status, detail)
        status ∈ {"registered", "rejected", "error"}
    """
    cmd = [sys.executable, "-X", "utf8", str(ROOT / "main.py"), "add", url]
    try:
        proc = subprocess.run(
            cmd,
            cwd=ROOT,
            input="n\n",
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "error", f"subprocess 타임아웃 (>{timeout}s)"
    except Exception as e:
        return "error", f"{type(e).__name__}: {e}"

    combined = (proc.stdout or "") + (proc.stderr or "")
    tail = combined[-800:]  # 마지막 800자 (결과 판정용)

    if "[OK] 등록 완료" in combined:
        return "registered", tail.strip()
    if "[거부]" in combined or "validator 실패" in combined:
        return "rejected", tail.strip()
    if proc.returncode != 0:
        return "error", f"exit={proc.returncode} — {tail.strip()}"
    # 알 수 없는 종료: 출력에 성공/거부 마커 없음
    return "rejected", f"success 마커 없음 — {tail.strip()}"


def main():
    args = _parse_args()
    candidates = _load_candidates(args.category, args.limit)
    if not candidates:
        print("처리할 후보 없음 — --category 필터 확인.")
        return

    existing = _existing_urls()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    logs_dir = ROOT / "logs"
    logs_dir.mkdir(exist_ok=True)
    json_path = logs_dir / f"batch_add_{timestamp}.json"
    txt_path = logs_dir / f"batch_add_{timestamp}.txt"

    mode = "DRY RUN" if args.dry_run else "실제 add"
    print(f"=== 배치 add 시작 ({mode}): {len(candidates)} 개 ===")
    print(f"JSON → {json_path}")
    print(f"TXT  → {txt_path}")
    print(f"sites.json 기존 URL: {len(existing)} 개")
    print()

    results = []
    with open(txt_path, "w", encoding="utf-8") as txt_f:
        for idx, entry in enumerate(candidates, 1):
            name = entry["name"]
            url = entry["url"]
            category = entry.get("category", "?")
            header = f"[{idx}/{len(candidates)}] {category} · {name} · {url}"
            print(header)
            txt_f.write(f"\n{'=' * 70}\n{header}\n{'=' * 70}\n")
            txt_f.flush()

            record = {
                "idx": idx,
                "name": name,
                "url": url,
                "category": category,
            }

            # 1) skip 필드 (JSON 자체에서 스킵 의도)
            if entry.get("skip"):
                record["status"] = "skipped"
                record["detail"] = entry.get("skip_reason", "")
                print(f"  [SKIP] {record['detail']}")
                txt_f.write(f"SKIPPED: {record['detail']}\n")
                results.append(record)
                continue

            # 2) sites.json 에 동일 URL 존재
            if url in existing:
                record["status"] = "dup"
                record["detail"] = "sites.json 에 동일 URL 존재"
                print(f"  [DUP] 이미 등록된 URL")
                txt_f.write(f"DUP: 동일 URL 이 sites.json 에 존재\n")
                results.append(record)
                continue

            # 3) preflight — URL 도달성
            start = time.time()
            ok, reason = _preflight(url)
            preflight_elapsed = time.time() - start
            if not ok:
                record["status"] = "unreachable"
                record["detail"] = reason
                record["elapsed_sec"] = round(preflight_elapsed, 1)
                print(f"  [UNREACH] {reason}")
                txt_f.write(f"UNREACHABLE: {reason}\n")
                results.append(record)
                continue

            # 4) dry-run 이면 여기까지
            if args.dry_run:
                record["status"] = "preflight_ok"
                record["elapsed_sec"] = round(preflight_elapsed, 1)
                print(f"  [DRY] preflight OK ({preflight_elapsed:.1f}s)")
                txt_f.write(f"DRY: preflight OK\n")
                results.append(record)
                continue

            # 5) 실제 add 호출
            add_start = time.time()
            status, detail = _run_add(url, args.timeout)
            add_elapsed = time.time() - add_start
            record["status"] = status
            record["detail"] = detail
            record["elapsed_sec"] = round(preflight_elapsed + add_elapsed, 1)

            flag = {"registered": "OK", "rejected": "REJECT", "error": "ERROR"}[status]
            print(f"  [{flag}] ({add_elapsed:.0f}s)")
            txt_f.write(f"{flag} ({add_elapsed:.0f}s)\n{detail}\n")

            # 성공 시 existing 집합 갱신 (뒤에 나올 같은 URL 중복 방어)
            if status == "registered":
                existing.add(url)

            results.append(record)
            txt_f.flush()

            # 중간 저장 (인터럽트 대비)
            with open(json_path, "w", encoding="utf-8") as jf:
                json.dump(results, jf, ensure_ascii=False, indent=2)

    # 최종 저장 & 요약
    with open(json_path, "w", encoding="utf-8") as jf:
        json.dump(results, jf, ensure_ascii=False, indent=2)

    print("\n=== 요약 ===")
    from collections import Counter
    counts = Counter(r["status"] for r in results)
    for status, n in counts.most_common():
        print(f"  {status:15} {n}")

    if counts.get("registered"):
        print("\n-- 신규 등록 --")
        for r in results:
            if r["status"] == "registered":
                print(f"  {r['name']:20} {r['url']}")


if __name__ == "__main__":
    main()
