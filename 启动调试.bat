@echo off
chcp 65001 > nul
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    .venv\Scripts\python.exe main.py
) else if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe main.py
) else (
    python main.py
)

if errorlevel 1 (
    echo.
    echo 程序异常退出，错误码 %errorlevel%
    pause
)
