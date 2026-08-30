@echo off
setlocal
set SCRIPT_DIR=%~dp0
set OUTPUT_DIR=%~1
if "%OUTPUT_DIR%"=="" set OUTPUT_DIR=%SCRIPT_DIR%..\.poc-data\demo

python "%SCRIPT_DIR%run_demo.py" ^
  --database "%OUTPUT_DIR%\structural-index.sqlite" ^
  --json-output "%OUTPUT_DIR%\p1b-demo-result.json" ^
  --markdown-output "%OUTPUT_DIR%\p1b-demo-result.md"

endlocal
