@echo off
chcp 65001 >nul
title TraderBot — Backtest

:start
cls
echo ============================================================
echo   TraderBot — Интерактивный бэктест
echo ============================================================
echo.

:: Активировать виртуальное окружение
if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

:: ---- ВЫБОР ТИКЕРА ----
echo   Доступные тикеры:
echo.
echo     [1]  SBER       Сбербанк
echo     [2]  GAZP       Газпром
echo     [3]  GMKN       Норникель
echo     [4]  VTBR       ВТБ
echo     [5]  ROSN       Роснефть
echo     [6]  NVTK       НОВАТЭК
echo     [7]  TATN       Татнефть
echo.
set /p TICKER_INPUT=  Введите тикер (например SBER):
set TICKER=%TICKER_INPUT%

if /i "%TICKER%"=="1" set TICKER=SBER
if /i "%TICKER%"=="2" set TICKER=GAZP
if /i "%TICKER%"=="3" set TICKER=GMKN
if /i "%TICKER%"=="4" set TICKER=VTBR
if /i "%TICKER%"=="5" set TICKER=ROSN
if /i "%TICKER%"=="6" set TICKER=NVTK
if /i "%TICKER%"=="7" set TICKER=TATN

:: Проверить, что тикер не пустой
if "%TICKER%"=="" (
    echo.
    echo   [ОШИБКА] Тикер не указан.
    echo.
    pause
    goto start
)

echo.
echo ............................................................
echo.

:: ---- ВЫБОР СТРАТЕГИИ ----
echo   Доступные стратегии:
echo.
echo     [1]  ict                    ICT оригинальная (15-свечной свип)
echo     [2]  ict_v2_sw4_rr2         ICT V2 ^| sweep=4  ^| RR=2.0
echo     [3]  ict_v2_sw4_rr35        ICT V2 ^| sweep=4  ^| RR=3.5
echo     [4]  ict_v2_sw10_rr2        ICT V2 ^| sweep=10 ^| RR=2.0
echo     [5]  ict_v2_tester          ICT V2 Tester (экспериментальная)
echo     [6]  ict_gazp               ICT для Газпром
echo     [7]  tatn_strat             ICT V3 Pro (Татнефть)
echo     [8]  gmkn_pro_trend_strat   GMKN Pro Trend
echo     [9]  nvtk_pro_strategy      NVTK Pro
echo.
set /p STRATEGY_INPUT=  Введите стратегию (например ict_v2_sw10_rr2):
set STRATEGY=%STRATEGY_INPUT%

if "%STRATEGY_INPUT%"=="1" set STRATEGY=ict
if "%STRATEGY_INPUT%"=="2" set STRATEGY=ict_v2_sw4_rr2
if "%STRATEGY_INPUT%"=="3" set STRATEGY=ict_v2_sw4_rr35
if "%STRATEGY_INPUT%"=="4" set STRATEGY=ict_v2_sw10_rr2
if "%STRATEGY_INPUT%"=="5" set STRATEGY=ict_v2_tester
if "%STRATEGY_INPUT%"=="6" set STRATEGY=ict_gazp
if "%STRATEGY_INPUT%"=="7" set STRATEGY=tatn_strat
if "%STRATEGY_INPUT%"=="8" set STRATEGY=gmkn_pro_trend_strat
if "%STRATEGY_INPUT%"=="9" set STRATEGY=nvtk_pro_strategy

:: Проверить, что стратегия не пустая
if "%STRATEGY%"=="" (
    echo.
    echo   [ОШИБКА] Стратегия не указана.
    echo.
    pause
    goto start
)

echo.
echo ............................................................
echo.

:: ---- КОЛИЧЕСТВО ДНЕЙ ----
set /p DAYS_INPUT=  Количество дней (Enter = 30):
if "%DAYS_INPUT%"=="" (
    set DAYS=30
) else (
    set DAYS=%DAYS_INPUT%
)

echo.
echo ============================================================
echo   Тикер:      %TICKER%
echo   Стратегия:  %STRATEGY%
echo   Период:     %DAYS% дней
echo ============================================================
echo.

py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml --tickers %TICKER% --strategy %STRATEGY% --days %DAYS%

echo.
echo ============================================================
echo   Бэктест завершён. Результаты: traderbot\backtest\results\
echo ============================================================
echo.
echo   Нажмите любую клавишу чтобы запустить снова, или закройте окно.
pause >nul
goto start
