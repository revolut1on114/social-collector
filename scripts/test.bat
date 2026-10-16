@echo off
REM Офлайн-тести на фейковому Mastodon
chcp 65001 >nul
cd /d "%~dp0.."
if not exist .venv\Scripts\python.exe call scripts\setup.bat
.venv\Scripts\python.exe -m pytest -q
pause
