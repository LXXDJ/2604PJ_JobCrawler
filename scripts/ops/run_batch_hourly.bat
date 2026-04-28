@echo off
REM 위치: scripts/ops/run_batch_hourly.bat → 프로젝트 루트는 두 단계 위
REM python -u : 출력 unbuffered → logs\hourly_batch.log 가 실시간 라인 단위로 갱신
REM             (schtasks 가 vbs 통해 SW_HIDE 모드로 호출 → CMD 창은 안 뜸)
cd /d "%~dp0\..\.."
python -u -m scripts.ops.run_batch --no-detail >> logs\hourly_batch.log 2>&1
