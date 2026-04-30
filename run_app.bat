@echo off
setlocal

cd /d "%~dp0"

set "APP_HOST=127.0.0.1"
set "APP_PORT=8050"
set "EXE_PATH=%~dp0dist\ui_app\ui_app.exe"
set "UHD_DLL_DIR=C:\Program Files\UHD\bin"
set "UHD_IMAGES_DIR=C:\Program Files\UHD\share\uhd\images"

if not exist "%EXE_PATH%" (
    echo Error: Packaged app not found at:
    echo   "%EXE_PATH%"
    echo Run build_ui_app_exe.bat first.
    pause
    goto :end
)

if not exist "%UHD_DLL_DIR%\uhd.dll" (
    echo Error: UHD runtime not found at:
    echo   "%UHD_DLL_DIR%\uhd.dll"
    pause
    goto :end
)

set "PATH=%UHD_DLL_DIR%;%PATH%"
set "UHD_IMAGES_DIR=%UHD_IMAGES_DIR%"

echo Launching packaged app:
echo   "%EXE_PATH%"
start "RX Spectrum Server" cmd /k ""%EXE_PATH%" --host %APP_HOST% --port %APP_PORT%"

timeout /t 3 /nobreak >nul
start "" "http://%APP_HOST%:%APP_PORT%"

:end
endlocal
