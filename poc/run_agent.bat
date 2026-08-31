@echo off
setlocal
set SCRIPT_DIR=%~dp0
set DATABASE_PATH=%~1
set QUESTION_TEXT=%~2
set NETWORK_FLAG=%~3

if "%DATABASE_PATH%"=="" goto usage
if "%QUESTION_TEXT%"=="" goto usage

python "%SCRIPT_DIR%run_agent.py" ^
  --database "%DATABASE_PATH%" ^
  --question "%QUESTION_TEXT%" ^
  %NETWORK_FLAG%
goto end

:usage
echo Usage: run_agent.bat "D:\path\structural-index.sqlite" "question" [--allow-network]
exit /b 2

:end
endlocal
