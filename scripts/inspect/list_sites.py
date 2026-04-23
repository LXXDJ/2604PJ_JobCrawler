import json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
d = json.load(open("data/sites.json", encoding="utf-8"))
for e in d:
    print(f"  {e['site_id']:<12} {e['extraction_method']:<15} {e.get('url','')[:70]}")
print(f"\n총 {len(d)} 개 사이트 등록됨")
