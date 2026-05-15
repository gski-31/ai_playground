@echo off
setlocal
cd /d "%~dp0"

set "PY_CMD="
where py >nul 2>&1 && set "PY_CMD=py -3"
if not defined PY_CMD (where python >nul 2>&1 && set "PY_CMD=python")
if not defined PY_CMD (where python3 >nul 2>&1 && set "PY_CMD=python3")

if not defined PY_CMD (
    echo Python not found on PATH.
    echo If python.exe is at a known location, run it once like:
    echo     "C:\path\to\python.exe" -m venv .venv
    echo and then re-run this script.
    exit /b 1
)

echo Using: %PY_CMD%

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY_CMD% -m venv .venv
    if errorlevel 1 (echo Failed to create venv & exit /b 1)
    call .venv\Scripts\activate.bat
    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt
    if errorlevel 1 (echo pip install failed & exit /b 1)
) else (
    call .venv\Scripts\activate.bat
)

python app.py
