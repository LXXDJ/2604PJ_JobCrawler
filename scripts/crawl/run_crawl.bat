@echo off
REM Task Scheduler 에서 호출되는 엔트리. cwd 맞추고 로그 남기며 main.py crawl 실행.
setlocal
cd /d "c:\Users\jack1\OneDrive\Documents\code\2604PJ_JobCrawler"

for /f "tokens=2 delims==" %%a in ('wmic OS Get localdatetime /value') do set DT=%%a
set LOG=logs\scheduled-%DT:~0,8%.log

echo ========================================== >> "%LOG%"
echo [START] %DT% >> "%LOG%"
"C:\Users\jack1\.pyenv\pyenv-win\versions\3.11.9\python.exe" main.py crawl >> "%LOG%" 2>&1
echo [END]   exitcode=%ERRORLEVEL% >> "%LOG%"
endlocal
