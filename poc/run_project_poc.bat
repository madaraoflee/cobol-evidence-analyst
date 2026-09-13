@echo off
setlocal
python "%~dp0run_project_poc.py" %*
set "PROJECT_POC_EXIT=%ERRORLEVEL%"
endlocal & exit /b %PROJECT_POC_EXIT%
