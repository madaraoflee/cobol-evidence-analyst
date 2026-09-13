@echo off
setlocal
python "%~dp0run_framework_paths.py" %*
set "FRAMEWORK_PATH_EXIT=%ERRORLEVEL%"
endlocal & exit /b %FRAMEWORK_PATH_EXIT%
