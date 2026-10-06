@echo off
REM ===== Kaliper -> SQL Server (DB qc) =====
cd /d %~dp0

set DB_SERVER=gsportal-DEV01
set DB_PORT=1443
set DB_DATABASE=qc
set DB_USER=dev01-bedul
REM Isi password di baris bawah (jangan dibagikan / jangan di-commit ke Git):
set DB_PASSWORD=ISI_PASSWORD_DI_SINI

python kaliper_app.py %*
pause
