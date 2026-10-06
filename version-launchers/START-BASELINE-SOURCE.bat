@echo off
setlocal DisableDelayedExpansion
title Business Workbench - Baseline Source
set "VERSION_ROOT=%~dp0baseline-source"
set "WORKBENCH_DATA_DIR="
set "AGENT_SETTINGS_PATH="
if not exist "%VERSION_ROOT%\poc\web_app.py" (
    echo Baseline source folder was not found.
    echo Expected: "%VERSION_ROOT%\poc\web_app.py"
    echo Place this launcher beside the baseline-source folder.
    pause
    exit /b 2
)
echo Starting BASELINE source at http://127.0.0.1:8765
echo Close this server with Ctrl+C before switching versions.
pushd "%VERSION_ROOT%" || exit /b 2
python "poc\web_app.py" --port 8765 %*
set "VERSION_EXIT=%errorlevel%"
popd
if not "%VERSION_EXIT%"=="0" pause
exit /b %VERSION_EXIT%
