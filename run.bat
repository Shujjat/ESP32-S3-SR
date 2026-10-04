@echo off
setlocal
cd /d "%~dp0"

REM Build / flash / monitor utility for ESP32-S3-SR.
REM
REM Usage:
REM   run.bat              Build, flash, then open serial monitor (default)
REM   run.bat flash        Build + flash only
REM   run.bat monitor      Serial monitor only @ 115200
REM   run.bat both         Same as default (flash then monitor)
REM
REM Override port:       set PORT=COM6
REM Force toolchain:     set TOOLCHAIN=idf   (or pio / arduino)
REM Prefer ESP-IDF 5.3+ (auto-detects common install paths / export.ps1).

set "PS1=%~dp0tools\flash.ps1"
if not exist "%PS1%" (
  echo Missing %PS1%
  exit /b 1
)

set "ACTION=-FlashMonitor"
set "PASS=%*"

if /I "%~1"=="flash" (
  set "ACTION="
  for /f "tokens=1*" %%a in ("%*") do set "PASS=%%b"
) else if /I "%~1"=="monitor" (
  set "ACTION=-Monitor"
  for /f "tokens=1*" %%a in ("%*") do set "PASS=%%b"
) else if /I "%~1"=="both" (
  set "ACTION=-FlashMonitor"
  for /f "tokens=1*" %%a in ("%*") do set "PASS=%%b"
) else if /I "%~1"=="help" (
  goto :usage
) else if /I "%~1"=="-h" (
  goto :usage
) else if /I "%~1"=="--help" (
  goto :usage
) else if "%~1"=="" (
  set "PASS="
)

set "EXTRA="
if /I "%TOOLCHAIN%"=="idf" set "EXTRA=-Target idf"
if /I "%TOOLCHAIN%"=="pio" set "EXTRA=-Target pio"
if /I "%TOOLCHAIN%"=="arduino" set "EXTRA=-Target arduino"

powershell -NoProfile -ExecutionPolicy Bypass -File "%PS1%" %ACTION% %EXTRA% %PASS%
exit /b %ERRORLEVEL%

:usage
echo.
echo Usage: run.bat [flash^|monitor^|both]
echo.
echo   (no args^) / both   Build, flash, then monitor
echo   flash               Build + flash only
echo   monitor             Serial monitor only
echo.
echo   set PORT=COM6       Override serial port
echo   set TOOLCHAIN=idf   Force toolchain (idf / pio / arduino)
echo.
exit /b 0
