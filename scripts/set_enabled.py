"""sites.json 의 특정 사이트에 enabled 플래그 설정."""
import sys, io, json

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# argv: set_enabled.py <false|true> site1 site2 ...
val = sys.argv[1].lower() == "true"
targets = set(sys.argv[2:])

with open("data/sites.json", encoding="utf-8") as f:
    entries = json.load(f)

changed = []
for e in entries:
    if e["site_id"] in targets:
        e["enabled"] = val
        changed.append(e["site_id"])

with open("data/sites.json", "w", encoding="utf-8") as f:
    json.dump(entries, f, ensure_ascii=False, indent=2)

print(f"enabled={val} 설정: {', '.join(changed)}")
