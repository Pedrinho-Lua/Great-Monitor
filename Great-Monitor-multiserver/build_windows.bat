@echo off
setlocal
cd /d "%~dp0"

where python >nul 2>nul
if errorlevel 1 (
    echo Python 3.10 or newer was not found. Install it from https://www.python.org/downloads/windows/
    exit /b 1
)

python -m pip install --upgrade -r requirements-build.txt
if errorlevel 1 exit /b 1

python -m PyInstaller --noconfirm --clean --windowed --onefile --name LibbysServerMonitor app.py
if errorlevel 1 exit /b 1

echo.
echo Build complete: %~dp0dist\LibbysServerMonitor.exe
