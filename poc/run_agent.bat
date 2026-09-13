@echo off
setlocal
if "%~1"=="" goto usage

python "%~dp0run_agent.py" %*
exit /b %errorlevel%

:usage
echo Usage: run_agent.bat "D:\path\structural-index.sqlite" "question" [--entry PROGRAM-ID] [--allow-network]
exit /b 2
