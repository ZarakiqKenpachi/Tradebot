@echo off
chcp 65001 >nul
title TraderBot — Live Trading

echo ============================================================
echo  TraderBot — запуск торгового бота
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
    echo [INFO] Активирую виртуальное окружение...
    call venv\Scripts\activate.bat
) else (
    echo [INFO] Виртуальное окружение не найдено, используется системный Python.
)

echo [INFO] Запуск бота...
echo [INFO] Остановка: Ctrl+C
echo ============================================================
echo.

:loop
py -3.12 -m traderbot.main
set EXIT_CODE=%ERRORLEVEL%

if %EXIT_CODE% == 0 (
    echo.
    echo [INFO] Бот остановлен нормально.
    goto end
)

echo.
echo [WARN] Бот завершился с кодом %EXIT_CODE%. Перезапуск через 10 секунд...
echo Нажмите Ctrl+C для отмены.
timeout /t 10 /nobreak >nul
echo.
goto loop

:end
echo.
pause
