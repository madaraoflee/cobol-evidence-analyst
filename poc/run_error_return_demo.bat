@echo off
setlocal
set "ERROR_RETURN_OUTPUT=%~1"
if not defined ERROR_RETURN_OUTPUT set "ERROR_RETURN_OUTPUT=%~dp0..\.poc-data\error-return-v5"

python "%~dp0error_return_demo.py" ^
  --database "%ERROR_RETURN_OUTPUT%\structural-index.sqlite" ^
  --json-output "%ERROR_RETURN_OUTPUT%\result.json" ^
  --markdown-output "%ERROR_RETURN_OUTPUT%\result.md"

set "ERROR_RETURN_EXIT=%ERRORLEVEL%"
endlocal & exit /b %ERROR_RETURN_EXIT%
