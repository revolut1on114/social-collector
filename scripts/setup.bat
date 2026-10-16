@echo off
REM Один раз: віртуальне середовище, залежності, .env з прикладу
chcp 65001 >nul
cd /d "%~dp0.."
where python >nul 2>&1 || (echo [!] Python не знайдено. Встановіть Python 3.10+ з python.org ^(галочка "Add to PATH"^). & pause & exit /b 1)
if not exist .venv\Scripts\python.exe python -m venv .venv
.venv\Scripts\python.exe -m pip install -q --upgrade pip
.venv\Scripts\python.exe -m pip install -q -r requirements-dev.txt || (echo [!] Не вдалося встановити залежності & pause & exit /b 1)
if not exist .env copy .env.example .env >nul
echo Готово: .venv створено, .env на місці.
