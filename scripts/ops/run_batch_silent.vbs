' schtasks 가 호출하는 invisible 래퍼.
' .bat 을 직접 실행하면 CMD 창이 잠깐 뜨므로,
' wscript.exe 를 통해 SW_HIDE(=0) 모드로 백그라운드 실행한다.
'
' Output 은 .bat 내부에서 logs\hourly_batch.log 로 redirect 됨.

Set Sh = CreateObject("WScript.Shell")

' __dirname 의 부모 = 프로젝트 루트
scriptPath = WScript.ScriptFullName
scriptDir  = Left(scriptPath, InStrRev(scriptPath, "\"))
batPath    = scriptDir & "run_batch_hourly.bat"

' 0=SW_HIDE  False=async (Wait 하지 않음, 다음 일정 사이클 안전)
Sh.Run """" & batPath & """", 0, False
