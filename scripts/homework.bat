@echo off
REM Реальний збір для здачі: тести -> run -> backfill до 200+ -> повторний run -> export + дашборд
chcp 65001 >nul
cd /d "%~dp0.."
call scripts\setup.bat || (pause & exit /b 1)
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m collector homework --min 200
echo EXIT=%errorlevel% > data\homework_exit.txt
start "" data\export\dashboard.html
echo.
echo Готово. Результати: data\export
pause
