@echo off
setlocal
python "%~dp0web_app.py" %*
exit /b %errorlevel%
