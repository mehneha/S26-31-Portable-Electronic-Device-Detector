@echo off
setlocal
setlocal EnableDelayedExpansion

REM Run from this script's directory.
cd /d "%~dp0"

set "APP_HOST=127.0.0.1"
set "APP_PORT=8050"
set "PY_CMD="
set "USE_PY_LAUNCHER=0"

REM Prefer known Python 3.12 path first.
if exist "C:\Users\evanc\AppData\Local\Programs\Python\Python312\python.exe" (
    set "PY_CMD=C:\Users\evanc\AppData\Local\Programs\Python\Python312\python.exe"
)

REM Fallback to py launcher.
if not defined PY_CMD (
    where py >nul 2>nul
    if !errorlevel! == 0 (
        set "PY_CMD=py"
        set "USE_PY_LAUNCHER=1"
    )
)

REM Fallback to python on PATH.
if not defined PY_CMD (
    where python >nul 2>nul
    if !errorlevel! == 0 set "PY_CMD=python"
)

if not defined PY_CMD (
    echo Error: Python was not found on PATH.
    pause
    goto :end
)

REM Verify Dash is available in this Python environment.
if "!USE_PY_LAUNCHER!"=="1" (
    py -3 -c "import dash" >nul 2>nul
) else (
    "%PY_CMD%" -c "import dash" >nul 2>nul
)
if not !errorlevel! == 0 (
    echo Error: Dash is not available in the selected Python environment.
    if "!USE_PY_LAUNCHER!"=="1" (
        echo Selected command: py -3
    ) else (
        echo Selected command: "%PY_CMD%"
    )
    pause
    goto :end
)

REM Start app server in its own persistent console.
if "!USE_PY_LAUNCHER!"=="1" (
    start "RX Spectrum Server" cmd /k "py -3 ui_app.py --host %APP_HOST% --port %APP_PORT%"
) else (
    start "RX Spectrum Server" cmd /k ""%PY_CMD%" ui_app.py --host %APP_HOST% --port %APP_PORT%"
)

REM Give server a moment to come up, then open browser.
timeout /t 3 /nobreak >nul
start "" "http://%APP_HOST%:%APP_PORT%"

:end
endlocal
