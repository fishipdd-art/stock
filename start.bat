@echo off
REM Windows startup script
SETLOCAL ENABLEDELAYEDEXPANSION

SET SCRIPT_DIR=%~dp0
cd /d %SCRIPT_DIR%

IF NOT EXIST ".venv\Scripts\python.exe" (
    echo [ERROR] Virtual environment not found at %SCRIPT_DIR%\.venv
    echo Run: uv venv --python 3.11 .venv ^&^& uv pip install --python .venv\Scripts\python.exe -r requirements.txt
    exit /b 1
)

SET PY=.venv\Scripts\python.exe

IF "%1"=="" GOTO start
IF "%1"=="start" GOTO start
IF "%1"=="init" GOTO init
IF "%1"=="run" GOTO run
IF "%1"=="once" GOTO once
IF "%1"=="backfill" GOTO backfill
IF "%1"=="report" GOTO report
IF "%1"=="stats" GOTO stats
IF "%1"=="web" GOTO web
IF "%1"=="collect" GOTO collect
IF "%1"=="service-install" GOTO service_install
IF "%1"=="service-uninstall" GOTO service_uninstall

echo Usage: %~nx0 {start^|init^|run^|once^|backfill^|report^|stats^|web^|collect^|service-install^|service-uninstall}
exit /b 1

:start
echo Starting scheduler + web (Ctrl+C to stop)...
%PY% main.py start %2 %3 %4
GOTO :eof

:init
%PY% main.py init
GOTO :eof

:run
echo Starting scheduler only (Ctrl+C to stop)...
%PY% main.py run
GOTO :eof

:once
echo Running all jobs once (smoke test)...
%PY% main.py once
GOTO :eof

:backfill
%PY% main.py backfill %2 %3 %4 %5 %6 %7
GOTO :eof

:report
%PY% main.py report
GOTO :eof

:stats
%PY% main.py stats
GOTO :eof

:web
echo Starting web server at http://localhost:8000
%PY% main.py web %2 %3 %4
GOTO :eof

:collect
%PY% main.py collect %2 %3 %4 %5
GOTO :eof

:service_install
%PY% scripts\install_windows_service.py install
GOTO :eof

:service_uninstall
%PY% scripts\install_windows_service.py uninstall
GOTO :eof