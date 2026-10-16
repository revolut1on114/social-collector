@echo off
REM Одноразовий добір історії назад від найстарішого зібраного поста
chcp 65001 >nul
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe call scripts\setup.bat
.venv\Scripts\python.exe -m collector backfill
.venv\Scripts\python.exe -m collector export >nul
pause
