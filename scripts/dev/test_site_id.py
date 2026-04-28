"""extract_site_id() ccTLD 처리 검증."""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "crawlers"))

from sites_registry import extract_site_id

cases = [
    "https://job.career.co.kr/jobs/",
    "https://www.hanin.or.kr/",
    "https://www.jobkorea.co.kr/",
    "https://api.camhr.com/",
    "https://siemreap.korean.net/",
    "https://careers.lg.com/",
    "https://www.hibrain.net/",
    "https://job.incruit.com/",
    "https://recruit.kt.com/",
    "https://www.peoplenjob.com/",
]
for u in cases:
    print(f"  {u:<40} -> {extract_site_id(u)}")
