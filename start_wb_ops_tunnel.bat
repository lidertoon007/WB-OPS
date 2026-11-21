@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

cd /d "%~dp0"
set "KEY=%USERPROFILE%\.ssh\id_ed25519"

if not exist "%KEY%" (
  echo Не найден SSH-ключ: %KEY%
  echo Проверьте, что ключ находится в папке .ssh.
  pause
  exit /b 1
)

curl.exe --noproxy "*" -fsS --max-time 3 http://127.0.0.1:8080/health >nul 2>nul
if not errorlevel 1 (
  echo Туннель WB OPS уже работает: http://127.0.0.1:8080/
  pause
  exit /b 0
)

rem Bind SSH to a physical interface so VPN/proxy adapters cannot stall the handshake.
set "FOUND_SOURCE="
for /f "delims=" %%I in ('powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\get_tunnel_source_ips.ps1"') do (
  set "FOUND_SOURCE=1"
  echo Подключаюсь к серверу через %%I...
  echo После подключения откройте: http://127.0.0.1:8080/
  echo Это окно не закрывайте, пока работает панель.
  echo.
  ssh -b %%I -p 443 -i "%KEY%" -N ^
    -o ConnectTimeout=10 ^
    -o ExitOnForwardFailure=yes ^
    -o ServerAliveInterval=30 ^
    -o ServerAliveCountMax=3 ^
    -L 8080:127.0.0.1:80 ^
    ubuntu@185.149.145.12
  echo.
  echo Соединение через %%I завершилось. Пробую следующее подключение, если оно есть.
)

if not defined FOUND_SOURCE echo Не найдено активное подключение Ethernet или Wi-Fi с IPv4 и шлюзом.
echo Не удалось запустить туннель. Проверьте сеть, SSH-ключ и свободен ли порт 8080.
pause
exit /b 1
