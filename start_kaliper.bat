@echo off
REM ===== Kaliper -> CSV lokal (koneksi PostgreSQL nonaktif secara default) =====
cd /d %~dp0

set DB_ENABLED=0

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" kaliper_app.py %*
) else (
    py -3 kaliper_app.py %* 2>nul || python kaliper_app.py %*
)
pause
