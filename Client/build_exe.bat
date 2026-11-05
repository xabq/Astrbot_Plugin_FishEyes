@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo   Building AstrBot Screen Agent Standalone EXE
echo ============================================================

set "PYTHON_EXE="

:: 1. Try configured python on system
where python >nul 2>nul
if %errorlevel% equ 0 (
    python -c "import sys; sys.exit(0)" >nul 2>nul
    if %errorlevel% equ 0 (
        set "PYTHON_EXE=python"
    )
)

:: 2. Try F:\python3.14.6\python.exe
if "!PYTHON_EXE!"=="" (
    if exist "F:\python3.14.6\python.exe" (
        set "PYTHON_EXE=F:\python3.14.6\python.exe"
    )
)

:: 3. Try py launcher
if "!PYTHON_EXE!"=="" (
    where py >nul 2>nul
    if %errorlevel% equ 0 (
        set "PYTHON_EXE=py"
    )
)

if "!PYTHON_EXE!"=="" (
    echo [ERROR] No working Python environment found!
    echo Please ensure Python is installed and added to PATH.
    pause
    exit /b 1
)

echo [*] Using Python: !PYTHON_EXE!

echo.
echo [*] Checking and installing dependencies from requirements.txt...
"!PYTHON_EXE!" -m pip install -r requirements.txt

echo.
echo [*] Executing build_exe.py with PyInstaller...
set "PYTHONIOENCODING=utf-8"
set "PYTHONUTF8=1"
"!PYTHON_EXE!" -X utf8 build_exe.py

echo.
echo ============================================================
echo   Finished! Press any key to exit.
echo ============================================================
pause
