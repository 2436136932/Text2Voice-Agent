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

REM ---- free the ports first ----
REM /T is essential: the services run behind `cmd /c` wrappers, so killing only
REM the port owner can leave the real python/node alive. The survivor then
REM competes for the same port and the browser sees ERR_CONNECTION_RESET.
for %%P in (8000 5173) do (
    for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%%P " ^| findstr "LISTENING"') do (
        echo [*] Port %%P busy ^(PID %%a^), stopping it first...
        taskkill /PID %%a /T /F >nul 2>&1
    )
)
REM Give the OS a moment to release the sockets.
timeout /t 2 /nobreak >nul

REM Verify the ports really are free before starting, so we never end up with
REM two backends fighting over port 8000.
set "busy="
for %%P in (8000 5173) do (
    netstat -ano 2>nul | findstr ":%%P " | findstr "LISTENING" >nul
    if not errorlevel 1 set "busy=1"
)
if defined busy (
    echo [X] Ports are still in use after cleanup. Please run stop.bat, then retry.
    pause
    exit /b 1
)

REM ---- launch via VBS (completely silent, no cmd windows) ----
cscript //nologo "%ROOT%scripts\launch.vbs"

REM ---- wait for services to come up ----
echo [*] Waiting for backend...
set "wait=0"
:wait_backend
timeout /t 1 /nobreak >nul
set /a wait+=1
REM A listening socket is not proof of a working API: a half-dead process still
REM holds the port. Probe /health over HTTP so a broken backend is caught here
REM instead of surfacing later in the browser as ERR_CONNECTION_RESET.
curl -fsS -m 3 http://127.0.0.1:8000/health >nul 2>&1
if not errorlevel 1 (
    echo [OK] Backend is healthy at http://localhost:8000
    goto wait_frontend
)
if %wait% lss 45 goto wait_backend
echo [!] Backend did not become healthy within 45s.
echo     Check the log: backend\logs\backend.log
set "backend_failed=1"

:wait_frontend
echo [*] Waiting for frontend...
set "wait=0"
:wait_fe_loop
timeout /t 1 /nobreak >nul
set /a wait+=1
REM Probe by hostname, not 127.0.0.1: Vite binds IPv6-only (::1) by default, so
REM an IPv4 probe reports a false failure even though the server is fine.
curl -fsS -m 3 http://localhost:5173/ >nul 2>&1
if not errorlevel 1 (
    echo [OK] Frontend is running at http://localhost:5173
    goto started
)
if %wait% lss 45 goto wait_fe_loop
echo [!] Frontend did not start within 45s. Check frontend\logs\frontend.log
set "frontend_failed=1"

:started
echo.
if defined backend_failed (
    echo ============================================
    echo   [!] BACKEND FAILED TO START
    echo.
    echo   The UI will load but every action will fail.
    echo   Last log lines:
    echo   ------------------------------------------
    powershell -NoProfile -Command "Get-Content '%~dp0backend\logs\backend.log' -Tail 12 -ErrorAction SilentlyContinue"
    echo   ------------------------------------------
    echo.
    echo   Common causes: port still held by a stale process
    echo   ^(run stop.bat^), or a missing dependency.
    echo ============================================
    echo.
    pause
    endlocal
    exit /b 1
)
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

REM ---- open browser ----
start "" "http://localhost:5173"

echo Opening browser... This window will close in 3 seconds...
timeout /t 3 /nobreak >nul
endlocal
exit /b 0
