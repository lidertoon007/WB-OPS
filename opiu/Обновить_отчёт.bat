@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Обновление ОПиУ по всем маркетплейсам и кабинетам. Для Ozon отключите VPN.
echo.
for /f %%m in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM"') do set "CUR=%%m"
echo Юнит-экономика по артикулам - за какой месяц собрать?
echo   Enter     - текущий месяц (%CUR%)
echo   ГГГГ-ММ   - другой месяц, например 2026-08
echo   0         - весь период свода
set "UNIT="
set /p "UNIT=Месяц: "
if "%UNIT%"=="" set "UNIT=%CUR%"
echo.
python opiu_all.py --open --unit-month %UNIT% %*
echo.
pause
