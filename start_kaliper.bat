@echo off
REM ===== Kaliper -> SQL Server (DB qc) =====
cd /d %~dp0

set DB_SERVER=gsportal-DEV01
set DB_PORT=1433
set DB_DATABASE=qc
set DB_USER=dev01-bedul
REM Isi password di baris bawah (jangan dibagikan / jangan di-commit ke Git):
set DB_PASSWORD=JanganMainMalamMalam@2016!

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" kaliper_app.py %*
) else (
    py -3 kaliper_app.py %* 2>nul || python kaliper_app.py %*
)
pause
