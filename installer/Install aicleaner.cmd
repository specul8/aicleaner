@echo off
rem aicleaner setup (feature 104). Double-click to install. It runs Deploy-AiCleaner.ps1, which sits
rem beside this file, with Windows PowerShell - present on every Windows 10 and 11 - for this one run
rem only (-ExecutionPolicy Bypass changes nothing on the machine). The files are unblocked first:
rem Windows marks what came from the internet, and PowerShell would refuse to run them otherwise.
setlocal
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0' -File | Unblock-File"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Deploy-AiCleaner.ps1" %*
set "code=%errorlevel%"
echo.
if not "%code%"=="0" (
    echo Setup stopped with an error. The log is Deploy-AiCleaner.log, beside this file.
) else (
    echo Setup finished.
)
pause
exit /b %code%
