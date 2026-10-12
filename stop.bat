@echo off
chcp 65001 >nul
title Text2Voice: stop

echo ============================================
echo   Text2Voice Agent - stop
echo ============================================
echo.

set "killed=0"

REM ---- kill backend (port 8000) ----
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8000 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
    echo [v] Killed backend process (PID %%a).
    set "killed=1"
)

REM ---- kill frontend dev server (port 5173) ----
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5173 " ^| findstr "LISTENING"') do (
    taskkill /PID %%a /F >nul 2>&1
    echo [v] Killed frontend process (PID %%a).
    set "killed=1"
)

REM ---- close the launcher windows ----
taskkill /FI "WINDOWTITLE eq Text2Voice Backend*" /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Text2Voice Frontend*" /F >nul 2>&1

if "%killed%"=="0" (
    echo [i] No running services found.
)

echo.
echo Done.
timeout /t 3 >nul