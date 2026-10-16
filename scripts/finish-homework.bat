@echo off
REM Закриває чекліст здачі ДЗ-04: тести, реальний збір 200+ записів, експорт, дашборд, коміт, push.
chcp 65001 >nul
cd /d "%~dp0.."

echo === [1/4] Середовище ===
call scripts\setup.bat || exit /b 1

echo.
echo === [2/4] Тести ===
.venv\Scripts\python.exe -m pytest -q || (echo [!] Тести впали & pause & exit /b 1)

echo.
echo === [3/4] Збір: run -^> backfill до 200+ -^> повторний run -^> export ===
.venv\Scripts\python.exe -m collector homework --min 200
if errorlevel 1 (echo [!] Зібрано менше 200 - див. лог вище і data\logs\collector.log & pause & exit /b 1)
start "" data\export\dashboard.html

echo.
echo === [4/4] Коміт і push ===
where git >nul 2>&1 || (echo [!] git не встановлений & pause & exit /b 1)
if not exist .git git init -q -b main
git add -A
git commit -q -m "Зібрані дані (SQLite + JSONL/CSV), дашборд і звіт" && echo Закомічено.
git remote get-url origin >nul 2>&1
if errorlevel 1 (
  echo GitHub-репозиторій ще не підключений. Виконайте один раз:
  echo   gh repo create social-collector --public --source . --push
  echo або створіть порожній repo на github.com і:
  echo   git remote add origin https://github.com/^<user^>/social-collector.git
  echo   git push -u origin main
) else (
  git push -u origin HEAD
)
echo.
echo Готово.
pause
