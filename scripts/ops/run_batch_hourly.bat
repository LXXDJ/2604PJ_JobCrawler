@echo off
REM 위치: scripts/ops/run_batch_hourly.bat → 프로젝트 루트는 두 단계 위
cd /d "%~dp0\..\.."
python -m scripts.ops.run_batch --no-detail >> logs\hourly_batch.log 2>&1
