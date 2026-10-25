@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ОПиУ по неделям — все маркетплейсы и кабинеты, колонки недели (пн-вс). Для Ozon отключите VPN.
echo.
echo Собираю за весь период свода. Первая и последняя недели могут захватывать дни соседних месяцев.
echo.
python opiu_all.py --grain week --open %*
echo.
pause
