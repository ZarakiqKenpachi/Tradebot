@echo off
chcp 65001 >nul
title TraderBot — Проверка подключения

echo ============================================================
echo  TraderBot — диагностика перед запуском
echo ============================================================
echo.

:: Проверить наличие .env файла
if not exist "traderbot\.env" (
    echo [ОШИБКА] Файл traderbot\.env не найден!
    echo Скопируйте traderbot\.env.example в traderbot\.env и заполните токены.
    echo.
    pause
    exit /b 1
)

:: Активировать виртуальное окружение, если есть
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

echo [INFO] Проверка подключения к T-Bank и Telegram...
echo.

py -3.12 -m traderbot.check

echo.
pause
