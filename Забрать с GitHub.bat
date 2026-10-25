@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title WB OPS - забрать с GitHub
where git >nul 2>nul || (echo Git не установлен: https://git-scm.com/download/win & pause & exit /b 1)
echo Забираю свежую версию с GitHub...
git pull --no-rebase --no-edit
if errorlevel 1 (
    echo.
    echo Не получилось: на этом устройстве есть свои несохранённые изменения.
    echo Сначала запустите «Отправить на GitHub.bat», потом этот файл.
    pause
    exit /b 1
)
echo Готово. Перезапустите панель, чтобы изменения заработали.
pause
