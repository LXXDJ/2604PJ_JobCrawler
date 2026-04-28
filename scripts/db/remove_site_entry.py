"""sites.json 에서 site_id 로 엔트리 제거 (DB 데이터는 유지).

사용: python scripts/db/remove_site_entry.py <site_id>
"""
import io
import json
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent.parent
path = ROOT / "data" / "sites.json"

target = sys.argv[1] if len(sys.argv) > 1 else None
if not target:
    print("Usage: python remove_site_entry.py <site_id>")
    sys.exit(1)

data = json.loads(path.read_text(encoding="utf-8"))
before = len(data)
data = [e for e in data if e.get("site_id") != target]
after = len(data)
if before == after:
    print(f"site_id='{target}' not found in sites.json")
    sys.exit(1)

# 백업
backup = path.with_suffix(".json.bak")
backup.write_text(json.dumps(json.loads(path.read_text(encoding="utf-8")), indent=2, ensure_ascii=False), encoding="utf-8")
path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

print(f"Removed '{target}' ({before} → {after} entries). Backup: {backup.name}")
