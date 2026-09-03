@echo off
chcp 65001 >nul
cd /d "%~dp0"
if "%1"=="test" (
    ".venv\Scripts\python.exe" test_draw.py
    pause
    exit /b
)
".venv\Scripts\python.exe" main.py
pause