@echo off
setlocal
set "COMPLEX_DEMO_OUTPUT=%~1"
if not defined COMPLEX_DEMO_OUTPUT set "COMPLEX_DEMO_OUTPUT=%~dp0..\.poc-data\complex-v2"

python "%~dp0complex_demo.py" ^
  --database "%COMPLEX_DEMO_OUTPUT%\structural-index.sqlite" ^
  --json-output "%COMPLEX_DEMO_OUTPUT%\result.json" ^
  --markdown-output "%COMPLEX_DEMO_OUTPUT%\result.md"

set "COMPLEX_DEMO_EXIT=%ERRORLEVEL%"
endlocal & exit /b %COMPLEX_DEMO_EXIT%
