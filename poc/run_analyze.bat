@echo off
setlocal
python "%~dp0analyze_source.py" %*
exit /b %ERRORLEVEL%
