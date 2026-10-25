@echo off
setlocal

cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo Не найдено виртуальное окружение .venv.
    echo Создайте его командой: python -m venv .venv
    pause
    exit /b 1
)

rem Доустановить зависимости проекта, если их нет (например, после обновления pyproject.toml)
".venv\Scripts\python.exe" -c "import openpyxl" 2>nul || ".venv\Scripts\python.exe" -m pip install -q -e .

echo Запуск WB OPS...
echo Откройте в браузере: http://127.0.0.1:8000
echo Для остановки нажмите Ctrl+C или закройте это окно.
echo.

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000

echo.
echo Сервер остановлен.
pause
