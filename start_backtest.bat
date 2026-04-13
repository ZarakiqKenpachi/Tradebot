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
echo     0  ALL (all tickers)
echo     1  SBER
echo     2  GAZP
echo     3  GMKN
echo     4  VTBR
echo     5  ROSN
echo     6  NVTK
echo     7  TATN
echo.
set /p TCHOICE=  Ticker (number or name):
if "%TCHOICE%"=="" goto start

if "%TCHOICE%"=="0" set TICKER=SBER,GAZP,GMKN,VTBR,ROSN,NVTK,TATN
if "%TCHOICE%"=="1" set TICKER=SBER
if "%TCHOICE%"=="2" set TICKER=GAZP
if "%TCHOICE%"=="3" set TICKER=GMKN
if "%TCHOICE%"=="4" set TICKER=VTBR
if "%TCHOICE%"=="5" set TICKER=ROSN
if "%TCHOICE%"=="6" set TICKER=NVTK
if "%TCHOICE%"=="7" set TICKER=TATN
rem If not a number, use as-is (typed ticker name or comma-separated list)
if not defined TICKER set TICKER=%TCHOICE%

echo.
echo   Strategies:
echo.
echo     1  ict
echo     2  ict_v2_sw4_rr2
echo     3  ict_v2_sw4_rr35
echo     4  ict_v2_sw10_rr2
echo     5  ict_v2_tester
echo     6  ict_gazp
echo     7  tatn_strat
echo     8  gmkn_pro_trend_strat
echo     9  nvtk_pro_strategy
echo     0  (from config - each ticker uses its own strategy)
echo.
set /p SCHOICE=  Strategy (number or name, Enter = from config):
if "%SCHOICE%"=="" set SCHOICE=0

set STRATEGY=
if "%SCHOICE%"=="1" set STRATEGY=ict
if "%SCHOICE%"=="2" set STRATEGY=ict_v2_sw4_rr2
if "%SCHOICE%"=="3" set STRATEGY=ict_v2_sw4_rr35
if "%SCHOICE%"=="4" set STRATEGY=ict_v2_sw10_rr2
if "%SCHOICE%"=="5" set STRATEGY=ict_v2_tester
if "%SCHOICE%"=="6" set STRATEGY=ict_gazp
if "%SCHOICE%"=="7" set STRATEGY=tatn_strat
if "%SCHOICE%"=="8" set STRATEGY=gmkn_pro_trend_strat
if "%SCHOICE%"=="9" set STRATEGY=nvtk_pro_strategy
if "%SCHOICE%"=="0" set STRATEGY=
rem If not a number, use as-is (typed strategy name)
if not defined STRATEGY if not "%SCHOICE%"=="0" set STRATEGY=%SCHOICE%

echo.
set /p DAYS=  Days (Enter = 30):
if "%DAYS%"=="" set DAYS=30

echo.
echo ============================================================
echo   Ticker:    %TICKER%
if defined STRATEGY (
    echo   Strategy:  %STRATEGY%
) else (
    echo   Strategy:  (from config)
)
echo   Days:      %DAYS%
echo ============================================================
echo.

if defined STRATEGY (
    py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml --tickers %TICKER% --strategy %STRATEGY% --days %DAYS%
) else (
    py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml --tickers %TICKER% --days %DAYS%
)

echo.
echo ============================================================
echo   Done. Results: traderbot\backtest\results\
echo ============================================================
echo.
pause
set TICKER=
set STRATEGY=
set TCHOICE=
set SCHOICE=
set DAYS=
goto start
