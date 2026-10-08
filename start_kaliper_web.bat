@echo off
REM ===== Kaliper WEB -> SQL Server (DB qc) =====
REM Memakai pengaturan DB yang sama dengan start_kaliper.bat (baris "set DB_..."),
REM sehingga password tidak perlu disalin ke file lain.
cd /d %~dp0

if not exist "%~dp0start_kaliper.bat" (
    echo [PERINGATAN] start_kaliper.bat tidak ditemukan - koneksi DB tidak diatur, aplikasi berjalan mode CSV saja.
) else (
    for /f "usebackq delims=" %%a in (`findstr /b /c:"set DB_" "%~dp0start_kaliper.bat"`) do %%a
)

set WEB_PORT=5000

start http://localhost:%WEB_PORT%

if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" web_app.py %*
) else (
    py -3 web_app.py %* 2>nul || python web_app.py %*
)
pause
