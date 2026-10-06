@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo Build computer only. On an office PC, open the packaged EXE instead.
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3.12 --version >nul 2>nul
if errorlevel 1 goto use_default_launcher
py -3.12 "%~dp0build_app.py" %*
goto done
:use_default_launcher
py -3 "%~dp0build_app.py" %*
goto done
:use_python
python "%~dp0build_app.py" %*
:done
set "BUILD_RESULT=%ERRORLEVEL%"
if not "%BUILD_RESULT%"=="0" echo Build failed. See docs\desktop-app.md. Build tools belong on an authorized build computer.
pause
exit /b %BUILD_RESULT%
