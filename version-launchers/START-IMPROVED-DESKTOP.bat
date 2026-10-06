@echo off
setlocal DisableDelayedExpansion
title Business Workbench - Improved Desktop
set "VERSION_ROOT=%~dp0improved-desktop"
set "VERSION_EXE="
set "VERSION_COUNT=0"
for %%F in ("%VERSION_ROOT%\COBOLWorkbench.exe" "%VERSION_ROOT%\COBOLWorkbench\COBOLWorkbench.exe" "%VERSION_ROOT%\COBOLWorkbench\COBOLWorkbench\COBOLWorkbench.exe") do (
    if exist "%%~fF" (
        set /a VERSION_COUNT+=1 >nul
        set "VERSION_EXE=%%~fF"
    )
)
if not "%VERSION_COUNT%"=="1" (
    echo Expected exactly one complete application in "%VERSION_ROOT%".
    echo Extract the improved Windows ZIP there, keeping all application files.
    echo Do not mix multiple builds or copy only the executable.
    pause
    exit /b 2
)
if not defined LOCALAPPDATA (
    echo LOCALAPPDATA is not set. The application was not started.
    pause
    exit /b 2
)
set "WORKBENCH_DATA_DIR=%LOCALAPPDATA%\COBOLWorkbench-improved"
set "AGENT_SETTINGS_PATH="
echo Starting IMPROVED desktop.
echo Local data: "%WORKBENCH_DATA_DIR%"
echo Close the application before switching versions.
start "" /wait "%VERSION_EXE%" %*
set "VERSION_EXIT=%errorlevel%"
if not "%VERSION_EXIT%"=="0" pause
exit /b %VERSION_EXIT%
