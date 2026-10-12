@echo off
chcp 65001 >nul 2>&1
title Text2Voice Agent - Start
setlocal

set "ROOT=%~dp0"
set "BACKEND=%ROOT%backend"
set "FRONTEND=%ROOT%frontend"
set "VENV=%ROOT%.venv"
set "PYW=%VENV%\Scripts\pythonw.exe"
set "PY=%VENV%\Scripts\python.exe"

echo ============================================
echo   Text2Voice Agent
echo ============================================
echo.

REM ---- check python ----
python --version >nul 2>&1
if errorlevel 1 (
    echo [X] Python not found. Please install Python 3.10+
    pause
    exit /b 1
)

REM ---- create venv + install backend deps (first run only) ----
if not exist "%PY%" (
    echo [*] Creating virtual environment...
    python -m venv "%VENV%"
    if errorlevel 1 (
        echo [X] Failed to create venv.
        pause
        exit /b 1
    )
    echo [*] Installing backend dependencies...
    "%PY%" -m pip install --upgrade pip -q
    "%PY%" -m pip install -r "%BACKEND%\requirements.txt" -q
    if errorlevel 1 (
        echo [X] Backend dependency installation failed.
        pause
        exit /b 1
    )
    echo [OK] Backend dependencies installed.
) else (
    echo [OK] Virtual environment found.
)

REM ---- check node ----
node --version >nul 2>&1
if errorlevel 1 (
    echo [X] Node.js not found. Please install Node.js 20+
    pause
    exit /b 1
)

REM ---- install frontend deps (first run only) ----
if not exist "%FRONTEND%\node_modules" (
    echo [*] Installing frontend dependencies...
    pushd "%FRONTEND%"
    call npm install
    popd
    if errorlevel 1 (
        echo [X] Frontend dependency installation failed.
        pause
        exit /b 1
    )
    echo [OK] Frontend dependencies installed.
) else (
    echo [OK] Frontend node_modules found.
)

echo.
echo [*] Starting services (no console windows)...

REM ---- kill anything on 8000 / 5173 first ----
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":8000 " ^| findstr "LISTENING"') do taskkill /PID %%a /F >nul 2>&1
for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":5173 " ^| findstr "LISTENING"') do taskkill /PID %%a /F >nul 2>&1

REM ---- launch via VBS (completely silent, no cmd windows) ----
cscript //nologo "%ROOT%scripts\launch.vbs"

REM ---- wait for services to come up ----
echo [*] Waiting for backend...
set "wait=0"
:wait_backend
timeout /t 1 /nobreak >nul
set /a wait+=1
netstat -ano 2>nul | findstr ":8000 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [OK] Backend is running at http://localhost:8000
    goto wait_frontend
)
if %wait% lss 30 goto wait_backend
echo [!] Backend did not start within 30s. Check backend\logs\backend.log

:wait_frontend
echo [*] Waiting for frontend...
set "wait=0"
:wait_fe_loop
timeout /t 1 /nobreak >nul
set /a wait+=1
netstat -ano 2>nul | findstr ":5173 " | findstr "LISTENING" >nul
if not errorlevel 1 (
    echo [OK] Frontend is running at http://localhost:5173
    goto started
)
if %wait% lss 30 goto wait_fe_loop
echo [!] Frontend did not start within 30s. Check frontend\logs\frontend.log

:started
echo.
echo ============================================
echo   Started successfully!
echo.
echo   Frontend UI :  http://localhost:5173
echo   Backend API :  http://localhost:8000/docs
echo   Backend log :  backend\logs\backend.log
echo   Frontend log:  frontend\logs\frontend.log
echo.
echo   To stop: run stop.bat
echo ============================================
echo.
echo This window will close in 5 seconds...
timeout /t 5 /nobreak >nul
endlocal
exit /b 0
