@echo off
cd /d "%~dp0"
set PYTHON=python

echo Checking dependencies...
"%PYTHON%" -c "import websockets" 2>nul
if errorlevel 1 (
    echo Installing the "websockets" package, one-time only...
    "%PYTHON%" -m pip install --user websockets
    if errorlevel 1 (
        echo.
        echo FAILED to install dependencies. See the error above.
        pause
        exit /b 1
    )
)

echo Starting PB tracker server...
start "PBTrackerServer" /min "%PYTHON%" server.py
timeout /t 2 /nobreak >nul

echo Opening browser...
start "" "http://localhost:4226"

echo.
echo PB tracker is running in the background (minimized window titled "PBTrackerServer").
echo Close that window, or press any key here, to stop it.
pause >nul

taskkill /F /FI "WINDOWTITLE eq PBTrackerServer*" >nul 2>&1
