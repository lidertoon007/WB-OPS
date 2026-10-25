@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Юнит-экономика по артикулам за выбранный месяц - все маркетплейсы и кабинеты. Для Ozon отключите VPN.
echo Сводный ОПиУ при этом не сохраняется - только файл unit_economics_ГГГГ-ММ.xlsx.
echo.
for /f %%m in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM"') do set "CUR=%%m"
for /f %%m in ('powershell -NoProfile -Command "(Get-Date).AddMonths(-1).ToString('yyyy-MM')"') do set "PREV=%%m"
echo За какой месяц собрать юнит-экономику?
echo   Enter     - текущий месяц (%CUR%)
echo   1         - прошлый месяц (%PREV%)
echo   ГГГГ-ММ   - другой месяц, например 2026-08
set "UNIT="
set /p "UNIT=Месяц: "
if "%UNIT%"=="" set "UNIT=%CUR%"
if "%UNIT%"=="1" set "UNIT=%PREV%"
echo.
python opiu_all.py --only-unit --open --unit-month %UNIT% %*
echo.
pause
