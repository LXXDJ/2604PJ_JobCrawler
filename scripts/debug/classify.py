"""홈 URL 받아 discover → classify 결과 출력 (수동 검증용)."""
from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

from crawlers.registration.menu_classifier import ClassifyInput, classify_batch
from crawlers.registration.menu_discovery import discover


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("home_url")
    ap.add_argument("--top", type=int, default=20,
                    help="discovery 상위 N개만 LLM 분류 (비용 절약)")
    ap.add_argument("--snippet", action="store_true",
                    help="각 후보 페이지 fetch 해서 본문 일부도 LLM 에 전달 (정확↑/속도↓)")
    args = ap.parse_args()

    d = discover(args.home_url)
    if not d.ok:
        print(f"discovery failed: {d.error}")
        return

    cands = d.candidates[: args.top]
    print(f"# discovery: {len(d.candidates)} candidates total, classifying top {len(cands)}")

    items = [ClassifyInput(url=c.url, text=c.text) for c in cands]
    results = classify_batch(items, fetch_snippet=args.snippet)

    by_label: dict[str, list] = {"full": [], "filtered": [], "personal": [], "unknown": []}
    for r in results:
        by_label[r.label].append(r)

    for label in ("full", "filtered", "personal", "unknown"):
        print(f"\n## {label} ({len(by_label[label])})")
        for r in by_label[label]:
            print(f"  - {r.url}")
            if r.reason:
                print(f"      reason: {r.reason}")


if __name__ == "__main__":
    main()
