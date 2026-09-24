@echo off
setlocal
set "NVG_PAUSE=1"
for %%A in (%*) do if /I "%%~A"=="-Check" set "NVG_PAUSE=0"
where powershell.exe >nul 2>&1
if errorlevel 1 (
  echo PowerShell was not found on this Windows installation.
  pause
  exit /b 1
)

powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\cleanup-windows.ps1" %*
set "NVG_EXIT=%ERRORLEVEL%"
if not "%NVG_EXIT%"=="0" (
  echo.
  echo Cleanup stopped. Nothing outside Narration Video Gen was removed.
)
if "%NVG_PAUSE%"=="1" (
  echo.
  pause
)
exit /b %NVG_EXIT%
