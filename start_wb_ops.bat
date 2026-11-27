@echo off
setlocal

cd /d "%~dp0"

rem Считать экземпляр актуальным только если отвечает также новый WB merges endpoint.
powershell -NoProfile -Command "try { $wbHealth = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/health' -TimeoutSec 2; if ($wbHealth.StatusCode -eq 200) { try { $wbMerges = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:8000/api/nomenclature/wb-merges' -TimeoutSec 2; if ($wbMerges.StatusCode -eq 200) { exit 0 } } catch {}; exit 2 } } catch {}; exit 1" >nul 2>&1
if not errorlevel 1 (
    echo WB OPS уже работает: http://127.0.0.1:8000
    echo Повторный запуск не требуется.
    exit /b 0
)
if errorlevel 2 (
    echo На порту 8000 отвечает устаревший экземпляр WB OPS.
    echo Остановите только свой старый процесс и запустите launcher повторно.
    exit /b 1
)

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

".venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

echo.
echo Сервер остановлен.
pause
