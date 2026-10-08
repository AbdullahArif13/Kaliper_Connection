@echo off
REM ===== Kaliper WEB -> CSV lokal (koneksi PostgreSQL nonaktif secara default) =====
cd /d %~dp0

set DB_ENABLED=0
set WEB_PORT=5000

start http://localhost:%WEB_PORT%

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" web_app.py %*
) else (
    py -3 web_app.py %* 2>nul || python web_app.py %*
)
pause
