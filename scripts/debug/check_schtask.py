"""schtask 상태 정확 출력 (cp949 디코드)."""
import io
import subprocess
import sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

for tn in ["JobCrawler", "JobCrawler_Batch", "JobCrawler_HalfHourlyBatch"]:
    res = subprocess.run(
        ["schtasks", "/Query", "/TN", tn, "/V", "/FO", "LIST"],
        capture_output=True, encoding="cp949", errors="replace",
    )
    print(f"\n=== {tn}  rc={res.returncode}")
    if res.stdout:
        print(res.stdout)
