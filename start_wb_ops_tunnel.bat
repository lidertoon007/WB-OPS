@echo off
setlocal
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

rem Happ VPN can capture the server IP. Add a temporary /32 physical route when happ-tun is active.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0ensure_netdata_route.ps1"
if errorlevel 1 (
  echo Не удалось подготовить маршрут к серверу WB OPS.
  echo Разрешите запрос Windows администратора и повторите запуск.
  pause
  exit /b 1
)

echo Подключаюсь к серверу...
echo После подключения откройте: http://127.0.0.1:8080/
echo Это окно не закрывайте, пока работает панель.
echo.

ssh -p 443 -i "%KEY%" -N ^
  -o ConnectTimeout=10 ^
  -o ExitOnForwardFailure=yes ^
  -o ServerAliveInterval=30 ^
  -o ServerAliveCountMax=3 ^
  -L 8080:127.0.0.1:80 ^
  ubuntu@185.149.145.12

echo.
echo Соединение с сервером завершилось.
pause
exit /b 1
