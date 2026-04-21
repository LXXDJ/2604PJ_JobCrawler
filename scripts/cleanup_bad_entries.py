"""sites.json 정리 — 잘못 잡힌 엔트리 제거 + site_id 수정.

실행: python scripts/cleanup_bad_entries.py
"""
import json
import os

PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "data", "sites.json")

with open(PATH, "r", encoding="utf-8") as f:
    entries = json.load(f)

removed = []
kept = []
for e in entries:
    # linkareer: graphql 필터 옵션 API (categoryIDs/regionIDs 샘플)
    # albamon: 브랜드 코드 API (스페셜 브랜드/일반음식점 샘플) — 둘 다 공고 아님
    if e["site_id"] in ("linkareer", "albamon"):
        removed.append(e["site_id"])
        continue
    kept.append(e)

# job.career.co.kr 의 자동 site_id 가 "co" 로 나와서 식별성 떨어짐 → career 로 변경
for e in kept:
    if e["site_id"] == "co":
        e["site_id"] = "career"

with open(PATH, "w", encoding="utf-8") as f:
    json.dump(kept, f, ensure_ascii=False, indent=2)

print("제거:", removed)
print("현재 사이트 수:", len(kept))
print("목록:", [e["site_id"] for e in kept])
