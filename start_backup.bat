@echo off
chcp 65001 >nul
title TraderBot — Резервное копирование

echo ============================================================
echo  TraderBot — резервная копия базы данных
echo ============================================================
echo.

set DB_SRC=traderbot\data\traderbot.db
set BACKUP_DIR=backups

:: Проверить наличие БД
if not exist "%DB_SRC%" (
    echo [ОШИБКА] База данных не найдена: %DB_SRC%
    echo Бот ещё не запускался или БД находится в другом месте.
    echo.
    pause
    exit /b 1
)

:: Создать папку для бэкапов
if not exist "%BACKUP_DIR%" mkdir "%BACKUP_DIR%"

:: Сформировать имя файла с датой и временем
:: Формат: traderbot_2026-04-07_15-30-00.db
for /f "tokens=1-3 delims=/" %%a in ("%DATE%") do (
    set DATE_FMT=%%c-%%b-%%a
)
for /f "tokens=1-3 delims=:." %%a in ("%TIME: =0%") do (
    set TIME_FMT=%%a-%%b-%%c
)
set BACKUP_FILE=%BACKUP_DIR%\traderbot_%DATE_FMT%_%TIME_FMT%.db

:: Копировать (sqlite3 в WAL-режиме — горячее копирование безопасно)
copy "%DB_SRC%" "%BACKUP_FILE%" >nul

if %ERRORLEVEL% == 0 (
    echo [OK] Копия сохранена: %BACKUP_FILE%
    echo.
    :: Показать все бэкапы и их размер
    echo Имеющиеся бэкапы:
    dir /b /o-d "%BACKUP_DIR%\traderbot_*.db" 2>nul
) else (
    echo [ОШИБКА] Не удалось скопировать файл.
)

echo.
pause
