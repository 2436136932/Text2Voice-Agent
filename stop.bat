@echo off
chcp 65001 >nul 2>&1
title Text2Voice Agent - Stop

echo ============================================
echo   Text2Voice Agent - Stop
echo ============================================
echo.

set "killed=0"

REM ---- kill by port, INCLUDING the child process tree ----
REM The backend/frontend are launched via `cmd /c ...` wrappers, so killing only
REM the pid that owns the port can leave the real python/node process alive.
REM A survivor then competes for the same port on the next start - which the
REM browser reports as ERR_CONNECTION_RESET. /T kills the whole tree.
for %%P in (8000 5173) do (
    for /f "tokens=5" %%a in ('netstat -ano 2^>nul ^| findstr ":%%P " ^| findstr "LISTENING"') do (
        taskkill /PID %%a /T /F >nul 2>&1
        echo [OK] Killed process tree on port %%P ^(PID %%a^)
        set "killed=1"
    )
)

REM ---- sweep any leftover project processes ----
REM Scoped to this project's path in the command line, so an unrelated python
REM job (a notebook, another service) is never touched.
for /f "skip=1 tokens=1" %%a in ('wmic process where "name='python.exe'" get processid 2^>nul') do (
    echo %%a| findstr /r "^[0-9][0-9]*$" >nul && (
        for /f "tokens=*" %%c in ('wmic process where "processid=%%a" get commandline 2^>nul ^| findstr /i "Text2Voice-Agent"') do (
            taskkill /PID %%a /T /F >nul 2>&1
            set "killed=1"
        )
    )
)
for /f "skip=1 tokens=1" %%a in ('wmic process where "name='node.exe'" get processid 2^>nul') do (
    echo %%a| findstr /r "^[0-9][0-9]*$" >nul && (
        for /f "tokens=*" %%c in ('wmic process where "processid=%%a" get commandline 2^>nul ^| findstr /i "Text2Voice-Agent"') do (
            taskkill /PID %%a /T /F >nul 2>&1
            set "killed=1"
        )
    )
)

REM ---- close the launcher windows ----
taskkill /FI "WINDOWTITLE eq Text2Voice Backend*" /T /F >nul 2>&1
taskkill /FI "WINDOWTITLE eq Text2Voice Frontend*" /T /F >nul 2>&1

if "%killed%"=="0" (
    echo [i] No running services found.
) else (
    echo.
    echo [OK] All Text2Voice services stopped.
)

REM ---- verify the ports are actually free before claiming success ----
timeout /t 2 /nobreak >nul
set "still="
for %%P in (8000 5173) do (
    netstat -ano 2>nul | findstr ":%%P " | findstr "LISTENING" >nul
    if not errorlevel 1 set "still=1"
)
if defined still (
    echo [!] Some ports are still held. Run stop.bat once more.
) else (
    echo [OK] Ports 8000 and 5173 are free.
)

echo.
echo Done.
timeout /t 3 >nul
