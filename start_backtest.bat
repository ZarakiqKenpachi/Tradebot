@echo off
chcp 65001 >nul
title TraderBot Backtest

:start
cls
echo ============================================================
echo   TraderBot - Backtest
echo ============================================================
echo.

if exist "venv\Scripts\activate.bat" (
    call venv\Scripts\activate.bat
)

echo   Tickers:
echo.
echo     SBER   GAZP   GMKN   VTBR   ROSN   NVTK   TATN
echo.
set /p TICKER=  Ticker:
if "%TICKER%"=="" goto start

echo.
echo   Strategies:
echo.
echo     ict
echo     ict_v2_sw4_rr2
echo     ict_v2_sw4_rr35
echo     ict_v2_sw10_rr2
echo     ict_v2_tester
echo     ict_gazp
echo     tatn_strat
echo     gmkn_pro_trend_strat
echo     nvtk_pro_strategy
echo.
set /p STRATEGY=  Strategy:
if "%STRATEGY%"=="" goto start

echo.
set /p DAYS=  Days (Enter = 30):
if "%DAYS%"=="" set DAYS=30

echo.
echo ============================================================
echo   Ticker:    %TICKER%
echo   Strategy:  %STRATEGY%
echo   Days:      %DAYS%
echo ============================================================
echo.

py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml --tickers %TICKER% --strategy %STRATEGY% --days %DAYS%

echo.
echo ============================================================
echo   Done. Results: traderbot\backtest\results\
echo ============================================================
echo.
pause
goto start
