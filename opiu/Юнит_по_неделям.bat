@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Юнит-экономика по НЕДЕЛЯМ - все маркетплейсы и кабинеты, колонки Год, Месяц, Неделя. Для Ozon отключите VPN.
echo Неделя на стыке месяцев делится на две строки, чтобы суммы месяца совпадали с месячной юниткой.
echo Сводный ОПиУ при этом не сохраняется - только файл unit_economics_weekly_ГГГГ-ММ_ГГГГ-ММ.xlsx.
echo.
echo С какого месяца собрать?
echo   Enter     - весь период свода (с 2026-01)
echo   ГГГГ-ММ   - начиная с месяца, например 2026-07
set "FROM="
set /p "FROM=Месяц: "
set "FROMARG="
if not "%FROM%"=="" set "FROMARG=--from %FROM%"
echo.
python opiu_all.py --unit-weekly --open %FROMARG% %*
echo.
pause
