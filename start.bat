@echo off
chcp 65001 >nul
title Text2Voice: start
setlocal

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "FRONTEND=%ROOT%frontend"
set "VENV=%ROOT%.venv"

echo ============================================
echo   Text2Voice Agent - 启动
echo ============================================
echo.

REM ---- check python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [X] Python not found. Please install Python 3.10+
    pause & exit /b 1
)

REM ---- create venv + install deps on first run ----
if not exist "%VENV%\Scripts\python.exe" (
    echo [*] Creating virtual environment ...
    python -m venv "%VENV%"
    echo [*] Installing backend dependencies (may take a few minutes) ...
    "%VENV%\Scripts\python.exe" -m pip install --upgrade pip -q
    "%VENV%\Scripts\python.exe" -m pip install -r "%BACKEND%\requirements.txt" -q
    if errorlevel 1 (
        echo [X] Backend dependency installation failed.
        pause & exit /b 1
    )
    echo [v] Backend dependencies installed.
) else (
    echo [v] Virtual environment found.
)

REM ---- check node ----
node --version >nul 2>&1
if errorlevel 1 (
    echo [X] Node.js not found. Please install Node.js 20+
    pause & exit /b 1
)

REM ---- install frontend deps on first run ----
if not exist "%FRONTEND%\node_modules" (
    echo [*] Installing frontend dependencies (may take a few minutes) ...
    cd /d "%FRONTEND%"
    call npm install
    if errorlevel 1 (
        echo [X] Frontend dependency installation failed.
        pause & exit /b 1
    )
    echo [v] Frontend dependencies installed.
) else (
    echo [v] Frontend node_modules found.
)

echo.
echo [*] Starting backend  (http://localhost:8000) ...
start "Text2Voice Backend" /D "%BACKEND%" cmd /k "%VENV%\Scripts\python.exe -m app.main"

echo [*] Starting frontend (http://localhost:5173) ...
start "Text2Voice Frontend" /D "%FRONTEND%" cmd /k "npm run dev"

echo.
echo ============================================
echo   Started successfully!
echo.
echo   Backend API :  http://localhost:8000/docs
echo   Frontend UI :  http://localhost:5173
echo.
echo   To stop: run stop.bat
echo ============================================
echo.
echo Press any key to close this window (services keep running) ...
pause >nul
endlocal