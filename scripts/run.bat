@echo off
REM Інкрементний прогін: лише нове після курсора
chcp 65001 >nul
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe call scripts\setup.bat
.venv\Scripts\python.exe -m collector run
.venv\Scripts\python.exe -m collector export >nul
pause
