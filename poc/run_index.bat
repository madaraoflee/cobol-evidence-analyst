@echo off
setlocal

if "%~1"=="" (
  echo Usage: run_index.bat SOURCE_FOLDER [DATABASE_PATH]
  exit /b 2
)

set "SOURCE_FOLDER=%~1"
set "DATABASE_PATH=%~2"
if "%DATABASE_PATH%"=="" set "DATABASE_PATH=%~dp0.poc-data\structural-index.sqlite"

python "%~dp0structural_index.py" "%SOURCE_FOLDER%" --database "%DATABASE_PATH%"
exit /b %ERRORLEVEL%
