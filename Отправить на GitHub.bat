@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
title WB OPS - отправка на GitHub

where git >nul 2>nul
if errorlevel 1 (
    echo Git не установлен. Скачайте и установите: https://git-scm.com/download/win
    pause
    exit /b 1
)
if not exist ".git" (
    echo Эта папка не подключена к GitHub.
    pause
    exit /b 1
)

echo [1/5] Резервная копия ключей и базы в папку backups...
set "STAMP=%date:~-4%-%date:~3,2%-%date:~0,2%_%time:~0,2%%time:~3,2%"
set "STAMP=%STAMP: =0%"
set "BK=backups\before_github_%STAMP%"
mkdir "%BK%" 2>nul
if exist ".env" copy /y ".env" "%BK%\" >nul
if exist "data" robocopy "data" "%BK%\data" /E /NFL /NDL /NJH /NJS /NP >nul
if exist "opiu" robocopy "opiu" "%BK%\opiu" *_accounts.json cost.csv /S /NFL /NDL /NJH /NJS /NP >nul
echo       готово: %BK%

echo [2/5] Собираю изменения...
git add -A

echo [3/5] Проверяю, что ключи и данные не попадут на GitHub...
git diff --cached --name-only > "%TEMP%\wbops_files.txt"
set BAD=0
findstr /I /L /C:"_accounts.json" /C:".sqlite3" /C:"cost.csv" /C:"/cache/" /C:"WBOPS_CABINETS/" /C:".bak" "%TEMP%\wbops_files.txt" >nul && set BAD=1
findstr /X /L /C:".env" "%TEMP%\wbops_files.txt" >nul && set BAD=1
findstr /B /L /C:"data/" /C:"backups/" "%TEMP%\wbops_files.txt" >nul && set BAD=1
if "%BAD%"=="1" (
    echo.
    echo СТОП: в отправку попали секретные файлы. Ничего не отправлено:
    findstr /I /L /C:"_accounts.json" /C:".sqlite3" /C:"cost.csv" /C:"/cache/" /C:"WBOPS_CABINETS/" /C:".bak" "%TEMP%\wbops_files.txt"
    findstr /X /L /C:".env" "%TEMP%\wbops_files.txt"
    findstr /B /L /C:"data/" /C:"backups/" "%TEMP%\wbops_files.txt"
    git reset -q
    pause
    exit /b 1
)
for /f %%n in ('type "%TEMP%\wbops_files.txt" ^| find /c /v ""') do set COUNT=%%n
echo       изменённых файлов: %COUNT%

git diff --cached --quiet
if errorlevel 1 (
    git commit -q -m "Обновление %date% %time:~0,5%"
    if errorlevel 1 (
        echo Не удалось сохранить изменения. Если Git просит имя и почту, выполните один раз:
        echo   git config --global user.name "Ваше имя"
        echo   git config --global user.email "почта от GitHub"
        pause
        exit /b 1
    )
) else (
    echo       новых изменений нет
)

echo [4/5] Забираю изменения с других устройств...
git pull --no-rebase --no-edit
if errorlevel 1 (
    echo.
    echo Изменения с другого устройства конфликтуют с вашими. Ничего не потеряно,
    echo но нужно объединить вручную: откройте папку в VS Code, вкладка Source Control.
    pause
    exit /b 1
)

echo [5/5] Отправляю на GitHub...
git push
if errorlevel 1 (
    echo.
    echo Не удалось отправить. Если открылось окно входа GitHub — войдите и запустите файл ещё раз.
    pause
    exit /b 1
)

echo.
echo Готово: все изменения на GitHub.
pause
