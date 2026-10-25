@echo off
chcp 65001 >nul
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Не найдено виртуальное окружение .venv.
    echo Создайте его командой: python -m venv .venv
    pause
    exit /b 1
)

".venv\Scripts\python.exe" -c "import openpyxl" 2>nul || ".venv\Scripts\python.exe" -m pip install -q -e .

echo Запуск WB OPS с доступом для команды...
echo.
echo  Вы на этом компьютере: http://127.0.0.1:8000
echo  Сотрудники входят по ссылке-приглашению из раздела «Моя команда».
echo  Без приглашения панель никого из сети не пустит.
echo.
echo  Если Windows спросит про доступ к сети — разрешите для ЧАСТНЫХ сетей.
echo  Для остановки нажмите Ctrl+C или закройте это окно.
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000

echo.
echo Сервер остановлен.
pause
