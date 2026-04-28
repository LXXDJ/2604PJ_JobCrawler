"""schtasks 등록 — JobCrawler_Batch (silent CMD).

wscript.exe + run_batch_silent.vbs 로 호출 → CMD 창 안 뜸.

사용:
  python -m scripts.ops.install_schtask              # 기본: 6시간마다
  python -m scripts.ops.install_schtask --hours 6
  python -m scripts.ops.install_schtask --minutes 30 # 30분마다 (옛 설정)
"""
import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VBS = ROOT / "scripts" / "ops" / "run_batch_silent.vbs"
TASK = "JobCrawler_Batch"
LEGACY_TASK = "JobCrawler_HalfHourlyBatch"  # 옛 이름 — 청소용


def _run(args):
    # 한국어 schtasks 출력 디코드 보호 (cp949)
    return subprocess.run(args, capture_output=True, encoding="cp949", errors="replace")


def main():
    if not VBS.exists():
        print(f"ERROR: VBS 없음: {VBS}", file=sys.stderr)
        sys.exit(1)

    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--hours", type=int, help="N시간마다 (HOURLY)")
    g.add_argument("--minutes", type=int, help="N분마다 (MINUTE)")
    args = ap.parse_args()

    # 기본 = 6시간
    if args.minutes is not None:
        sched_args = ["/SC", "MINUTE", "/MO", str(args.minutes)]
        label = f"{args.minutes}분"
    else:
        hours = args.hours if args.hours is not None else 6
        sched_args = ["/SC", "HOURLY", "/MO", str(hours)]
        label = f"{hours}시간"

    tr = f'wscript.exe "{VBS}"'

    # 기존 task 삭제 (현재 + 옛 이름)
    for tn in (TASK, LEGACY_TASK):
        _run(["schtasks", "/Delete", "/TN", tn, "/F"])

    # 재등록
    res = _run(["schtasks", "/Create", *sched_args,
                "/TN", TASK, "/TR", tr, "/F"])
    print(f"[create] rc={res.returncode}  ({label}마다)")
    print((res.stdout or "").strip())
    if res.stderr:
        print(res.stderr.strip())

    # 확인
    res = _run(["schtasks", "/Query", "/TN", TASK])
    print(f"[query] rc={res.returncode}")
    print((res.stdout or "")[:500])


if __name__ == "__main__":
    main()
