@echo off
setlocal
title Update Restia Docker Deployment

pushd "%~dp0" >nul

echo =========================================
echo Updating Restia Docker deployment
echo =========================================
echo.

where docker >nul 2>nul
if errorlevel 1 (
  echo [!] Docker was not found on PATH.
  echo     Start Docker Desktop, then run this script again.
  goto :fail
)

docker compose version >nul 2>nul
if errorlevel 1 (
  echo [!] Docker Compose is not available.
  echo     Update Docker Desktop, then run this script again.
  goto :fail
)

if "%RESTIA_IMAGE%"=="" set "RESTIA_IMAGE=ghcr.io/psmithul/restia:latest"
echo [+] Pulling %RESTIA_IMAGE%...
docker compose pull odysseus
if errorlevel 1 goto :fail

echo.
echo [+] Restarting Restia while preserving data and logs...
docker compose up -d --no-build odysseus
if errorlevel 1 goto :fail

echo.
echo [+] Removing dangling Docker images...
docker image prune -f
if errorlevel 1 goto :fail

echo.
echo =========================================
echo Update completed successfully.
echo =========================================
goto :done

:fail
echo.
echo Update failed. Check the message above and try again.

:done
popd >nul
pause
