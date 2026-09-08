@echo off
setlocal
set "CONTEXT_DEMO_OUTPUT=%~1"
if not defined CONTEXT_DEMO_OUTPUT set "CONTEXT_DEMO_OUTPUT=%~dp0..\.poc-data\context-v3"

python "%~dp0context_demo.py" ^
  --database "%CONTEXT_DEMO_OUTPUT%\structural-index.sqlite" ^
  --json-output "%CONTEXT_DEMO_OUTPUT%\result.json" ^
  --markdown-output "%CONTEXT_DEMO_OUTPUT%\result.md"

set "CONTEXT_DEMO_EXIT=%ERRORLEVEL%"
endlocal & exit /b %CONTEXT_DEMO_EXIT%
