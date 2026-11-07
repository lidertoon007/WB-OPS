@echo off
setlocal

cd /d "%~dp0"
set "KEY=%USERPROFILE%\.ssh\id_ed25519"

if not exist "%KEY%" (
  echo Не найден SSH-ключ: %KEY%
  echo Проверьте, что ключ находится в папке .ssh.
  pause
  exit /b 1
)

echo Запускаю SSH-туннель WB OPS...
echo После запуска откройте: http://127.0.0.1:8080/
echo Это окно не закрывайте, пока работает панель.
echo.

ssh -p 443 -i "%KEY%" -N ^
  -o ExitOnForwardFailure=yes ^
  -o ServerAliveInterval=30 ^
  -o ServerAliveCountMax=3 ^
  -L 8080:127.0.0.1:80 ^
  ubuntu@185.149.145.12

echo.
echo SSH-туннель остановлен.
pause
