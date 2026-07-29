@echo off
setlocal

echo.
echo  BCore Performance Dashboard
echo  ============================

:: ── 1. Check Python version ──────────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  ERROR: Python not found.
    echo  Install Python 3.13+ from https://www.python.org/downloads/
    echo  Make sure to check "Add Python to PATH" during installation.
    echo.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJ=%%a
    set PYMIN=%%b
)

if %PYMAJ% LSS 3 (
    echo.
    echo  ERROR: Python %PYVER% found but 3.13+ is required.
    echo  Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
if %PYMAJ% EQU 3 if %PYMIN% LSS 13 (
    echo.
    echo  ERROR: Python %PYVER% found but 3.13+ is required.
    echo  Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo  Python %PYVER% OK

:: ── 2. Create/activate virtual environment ───────────────────────────────────
if not exist "venv\Scripts\activate.bat" (
    echo  Creating virtual environment...
    python -m venv venv
    if errorlevel 1 (
        echo.
        echo  ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )
)

call venv\Scripts\activate.bat

:: ── 3. Install / update dependencies ─────────────────────────────────────────
echo  Checking dependencies...
pip install -q -r requirements.txt
if errorlevel 1 (
    echo.
    echo  ERROR: Dependency installation failed.
    echo  Try running: pip install -r requirements.txt
    pause
    exit /b 1
)

echo  Dependencies OK
echo.
echo  Starting server at http://localhost:5003
echo  Press Ctrl+C to stop.
echo.

:: ── 4. Start server ───────────────────────────────────────────────────────────
python serve.py

endlocal
