"""schtasks 등록 — JobCrawler_Batch (silent CMD).

wscript.exe + run_batch_silent.vbs 로 호출, CMD 창 안 뜸.
시작시각 /ST 00:00 으로 정각 anchor 정렬.

사용:
  python -m scripts.ops.install_schtask
"""
import io
import sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

# ════════════════════════════════════════════════════════════════════════════
#                       ⚙ 설정 (여기만 수정하면 됨)
# ════════════════════════════════════════════════════════════════════════════

# 배치 간격 — 두 값을 합산. (총 = HOURS*60 + MINUTES) 분.
# 둘 다 0 또는 None 이면 에러.
INTERVAL_HOURS   = 0       # 시 부분 (0, 1, 6, 12 등)
INTERVAL_MINUTES = 30      # 분 부분 (0, 30, 45 등 임의 값)

# 예시:
#   30분 마다             → HOURS=0, MINUTES=30  (00:00, 00:30, 01:00, ...)
#   매 정각              → HOURS=1, MINUTES=0   (00:00, 01:00, 02:00, ...)
#   3시간 30분마다        → HOURS=3, MINUTES=30  (00:00, 03:30, 07:00, ...)
#   6시간마다             → HOURS=6, MINUTES=0   (00, 06, 12, 18시)
#   12시간마다            → HOURS=12, MINUTES=0  (00, 12시)
#   45분마다              → HOURS=0, MINUTES=45  (00:00, 00:45, 01:30, ...)
#
# ⚠ 정확한 "정각 anchor" 가 필요하면 24시간을 정수 분할하는 값만 사용:
#   30, 60, 90, 120, 180, 240, 360, 720, 1440 분 등.
#   예: 3시간 30분(=210분) 은 24시간 (1440분) 으로 안 나눠떨어짐 → 매일
#     trigger 시간이 30분씩 밀림. 의도라면 OK.

# ════════════════════════════════════════════════════════════════════════════

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
VBS = ROOT / "scripts" / "ops" / "run_batch_silent.vbs"
TASK = "JobCrawler_Batch"
LEGACY_TASKS = ["JobCrawler_HalfHourlyBatch", "JobCrawler"]  # 옛 이름들 청소


def _run(args):
    # 한국어 schtasks 출력 디코드 보호 (cp949)
    return subprocess.run(args, capture_output=True, encoding="cp949", errors="replace")


def main():
    if not VBS.exists():
        print(f"ERROR: VBS 없음: {VBS}", file=sys.stderr)
        sys.exit(1)

    # 설정 검증 — 합산 분 계산
    total_min = (INTERVAL_HOURS or 0) * 60 + (INTERVAL_MINUTES or 0)
    if total_min <= 0:
        print("ERROR: INTERVAL_HOURS 와 INTERVAL_MINUTES 합이 0 이하 — 둘 다 0/None 임",
              file=sys.stderr)
        sys.exit(1)

    h, m = total_min // 60, total_min % 60
    label_parts = []
    if h:
        label_parts.append(f"{h}시간")
    if m:
        label_parts.append(f"{m}분")
    label = f"{' '.join(label_parts)} (총 {total_min}분, 정각 anchor)"

    sched_args = ["/SC", "MINUTE", "/MO", str(total_min), "/ST", "00:00"]

    tr = f'wscript.exe "{VBS}"'

    # 기존 task 삭제 (현재 + 옛 이름들)
    for tn in [TASK, *LEGACY_TASKS]:
        _run(["schtasks", "/Delete", "/TN", tn, "/F"])

    # 재등록
    res = _run(["schtasks", "/Create", *sched_args,
                "/TN", TASK, "/TR", tr, "/F"])
    print(f"[create] rc={res.returncode}  ({label})")
    if res.stdout:
        try:
            print(res.stdout.strip())
        except UnicodeEncodeError:
            print("[stdout] (cp949 decode skip, 등록 자체는 정상)")
    if res.stderr:
        try:
            print(res.stderr.strip())
        except UnicodeEncodeError:
            pass

    # 확인
    res = _run(["schtasks", "/Query", "/TN", TASK])
    print(f"[query] rc={res.returncode}")
    if res.stdout:
        try:
            print(res.stdout[:500])
        except UnicodeEncodeError:
            print("[query stdout] (cp949 decode skip)")


if __name__ == "__main__":
    main()
