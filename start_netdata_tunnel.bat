@echo off
setlocal

cd /d "%~dp0"
set "KEY=%USERPROFILE%\.ssh\id_ed25519"

if not exist "%KEY%" (
  echo SSH key not found: %KEY%
  pause
  exit /b 1
)

rem Happ VPN can capture the server IP without forwarding the SSH banner.
rem The helper adds a temporary /32 route through the physical gateway only
rem when happ-tun is active. Windows may show one UAC confirmation.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_netdata_route.ps1"
if errorlevel 1 (
  echo Could not prepare the route to the Netdata server.
  echo Approve the Windows administrator prompt and try again.
  pause
  exit /b 1
)

rem Refuse to replace or interfere with any listener already using this port.
powershell.exe -NoProfile -Command "if (Get-NetTCPConnection -LocalPort 19999 -ErrorAction SilentlyContinue) { exit 0 } else { exit 1 }"
if not errorlevel 1 (
  echo Port 19999 is already in use. Close the owning application or tunnel, then retry.
  pause
  exit /b 1
)

echo Starting the Netdata SSH tunnel...
echo The dashboard will open automatically when the tunnel is ready.
echo Full Netdata: http://127.0.0.1:19999/
echo Keep this window open while using Netdata.
echo.

rem A separate bounded watcher waits for the new listener before opening the browser.
start "" /min powershell.exe -NoProfile -WindowStyle Hidden -Command "$until=(Get-Date).AddSeconds(60); do { Start-Sleep -Seconds 1; $ready=Get-NetTCPConnection -State Listen -LocalPort 19999 -ErrorAction SilentlyContinue } until ($ready -or (Get-Date) -ge $until); if ($ready) { Start-Process 'http://127.0.0.1:19999/dashboard/' }"

ssh -p 443 -i "%KEY%" -N ^
  -o ExitOnForwardFailure=yes ^
  -o ConnectTimeout=15 ^
  -o ServerAliveInterval=30 ^
  -o ServerAliveCountMax=3 ^
  -L 19999:127.0.0.1:19998 ^
  ubuntu@185.149.145.12

echo.
if errorlevel 1 (
  echo Could not establish the Netdata SSH tunnel. Check SSH output above, key access, and network connectivity.
) else (
  echo Netdata SSH tunnel stopped.
)
pause
