"""schtasks 등록 — JobCrawler_HalfHourlyBatch (정각/30분, 완전 숨김).

wscript.exe + run_batch_silent.vbs 로 호출 → CMD 창 안 뜸.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VBS = ROOT / "scripts" / "ops" / "run_batch_silent.vbs"

if not VBS.exists():
    print(f"ERROR: VBS 없음: {VBS}", file=sys.stderr)
    sys.exit(1)

TR = f'wscript.exe "{VBS}"'
TASK = "JobCrawler_HalfHourlyBatch"

# 한국어 schtasks 출력 디코드 보호 (cp949).
def _run(args):
    return subprocess.run(args, capture_output=True, encoding="cp949", errors="replace")

# 기존 task 삭제 (없어도 OK)
_run(["schtasks", "/Delete", "/TN", TASK, "/F"])

# 재등록
res = _run(["schtasks", "/Create", "/SC", "MINUTE", "/MO", "30",
            "/TN", TASK, "/TR", TR, "/F"])
print(f"[create] rc={res.returncode}")
print((res.stdout or "").strip())
if res.stderr:
    print(res.stderr.strip())

# 확인
res = _run(["schtasks", "/Query", "/TN", TASK])
print(f"[query] rc={res.returncode}")
print((res.stdout or "")[:500])
