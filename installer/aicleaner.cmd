@echo off
rem aicleaner command wrapper: finds the venv (shared under %USERPROFILE%\.aicleaner, or repo-local .venv)
rem and runs "python -m aicleaner" with your arguments. Examples:
rem   aicleaner check
rem   aicleaner scan C:\repos\client-x --min-count 2
rem   aicleaner scan-tokens C:\repos\client-x
rem   aicleaner seed CLIENT05
setlocal
set "PY=%USERPROFILE%\.aicleaner\venv\Scripts\python.exe"
if not exist "%PY%" set "PY=%~dp0.venv\Scripts\python.exe"
if not exist "%PY%" (
  echo aicleaner: no venv found. Run Install-AiCleaner-VSCode.ps1 or Install-AiCleaner-Desktop.ps1 first. 1>&2
  exit /b 2
)
"%PY%" -m aicleaner %* --config "%~dp0aicleaner-config.json"
endlocal
