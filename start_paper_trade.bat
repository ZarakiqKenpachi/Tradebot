@echo off
chcp 65001 >nul
title TraderBot — Paper Trading (Sandbox)

echo ============================================================
echo  TraderBot — Paper Trading (Sandbox)
echo ============================================================
echo.

:: Активировать виртуальное окружение, если есть
if exist "venv\Scripts\activate.bat" (
    echo [INFO] Активирую виртуальное окружение...
    call venv\Scripts\activate.bat
) else (
    echo [INFO] Виртуальное окружение не найдено, используется системный Python.
)

echo [INFO] Запуск paper trading...
echo [INFO] Ордера размещаются в sandbox (без реальных денег)
echo [INFO] Остановка: Ctrl+C
echo ============================================================
echo.

py -3.12 -m traderbot.paper_trade %*

echo.
echo [INFO] Paper trading остановлен.
echo.
pause
