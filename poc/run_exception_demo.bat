@echo off
setlocal
set "EXCEPTION_DEMO_OUTPUT=%~1"
if not defined EXCEPTION_DEMO_OUTPUT set "EXCEPTION_DEMO_OUTPUT=%~dp0..\.poc-data\exception-v4"

python "%~dp0exception_demo.py" ^
  --database "%EXCEPTION_DEMO_OUTPUT%\structural-index.sqlite" ^
  --json-output "%EXCEPTION_DEMO_OUTPUT%\result.json" ^
  --markdown-output "%EXCEPTION_DEMO_OUTPUT%\result.md"

set "EXCEPTION_DEMO_EXIT=%ERRORLEVEL%"
endlocal & exit /b %EXCEPTION_DEMO_EXIT%
