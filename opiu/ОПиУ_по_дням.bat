@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ОПиУ по дням за один месяц — все маркетплейсы и кабинеты. Для Ozon отключите VPN.
echo.
for /f %%m in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM"') do set "CUR=%%m"
echo За какой месяц собрать отчёт по дням?
echo   Enter     - текущий месяц (%CUR%)
echo   ГГГГ-ММ   - другой месяц, например 2026-08
set "MON="
set /p "MON=Месяц: "
if "%MON%"=="" set "MON=%CUR%"
echo.
python opiu_all.py --grain day --period-month %MON% --open %*
echo.
pause
