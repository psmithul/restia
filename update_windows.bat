@echo off
setlocal
title Update Restia Docker Deployment
set "EXIT_CODE=0"

pushd "%~dp0" >nul

where docker >nul 2>nul
if errorlevel 1 (
  echo [!] Docker was not found on PATH. Start Docker Desktop, then retry.
  goto :fail
)

docker compose version >nul 2>nul
if errorlevel 1 (
  echo [!] Docker Compose is not available. Update Docker Desktop, then retry.
  goto :fail
)

where powershell >nul 2>nul
if errorlevel 1 (
  echo [!] Windows PowerShell is required for safe backup and rollback handling.
  goto :fail
)

powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0update_windows.ps1" %*
if errorlevel 1 goto :fail

echo.
echo =========================================
echo Update operation completed successfully.
echo =========================================
goto :done

:fail
set "EXIT_CODE=1"
echo.
echo Update failed. Restia keeps the verified pre-update snapshot and attempts
echo to restore the prior image whenever the new image does not become ready.

:done
popd >nul
pause
exit /b %EXIT_CODE%
