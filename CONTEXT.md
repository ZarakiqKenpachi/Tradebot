# TraderBot — Session Context

## Stack
- Python 3.12 (use `py -3.12` to run everything)
- Official T-Bank SDK: `t-tech-investments` from `https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple`
- Imports as `from t_tech.invest import ...`
- Sandbox endpoint: `sandbox-invest-public-api.tinkoff.ru:443`
- Live endpoint: `invest-public-api.tinkoff.ru:443`

## Commands
```
py -3.12 main.py           # run the live bot
py -3.12 backtest.py       # run backtest
py -3.12 -m pytest tests/ -v   # run tests
```

## .env
```
TINKOFF_TOKEN=t.tTrk5647EvT2NTYMzoSBzBqAP02xymUIVFFS1qwt4Ou9I3OSbwoE6yoas0X2VIv4KjggLPR_ZG94AE7K1yDDJg
SANDBOX=true
FIGI=BBG004730N88   # Sberbank
```
Sandbox account ID: `8c77a9bb-b8f5-4fd8-b909-005cca158d8b` (100k RUB funded)

## Strategy — ICT System Variant A
File: `bot/strategies/ict.py`

**1H timeframe (15 candles lookback):**
- Build structure window of last 15 closed 1H candles
- Bullish sweep: last candle wicks below structure low, closes back above
- Bearish sweep: last candle wicks above structure high, closes back below

**30m timeframe (after sweep only):**
- Only look at 30m candles that opened AT OR AFTER the sweep candle's open time
- Find first displacement candle: body >= 35% of range, in trade direction
- Entry = 50% retracement of that candle's body

**Risk:**
- Stop: 0.1% beyond the swept level
- Position size: 1% of account balance per trade
- RR: configurable, currently testing 1.5 and 2.0
- Timeout: 10 x 30m candles (5 hours), then exit at market

## Backtest Results (30 days, Sberbank BBG004730N88)
| RR  | Trades | Win%  | TP hits | Return   |
|-----|--------|-------|---------|----------|
| 1:1.5 | 27   | 66.7% | 11      | +17.96%  |
| 1:2.0 | 25   | 56.0% | 3       | +7.74%   |
| 1:3.5 | 57   | 40.4% | 1       | -8.49%   |

RR 1:1.5 with 15-candle lookback is the best so far.
Old 4-candle lookback had 57 trades and was unprofitable — too many false setups.

## What still needs testing
- Lookback values of 10 and 20 (user asked to find sweet spot)
- Different instruments (current results are Sberbank only)
- Timeout reduction from 10 to 5 bars

## Order placement
- Entry: limit order via `client.orders.post_order()`
- Stop-loss: real stop order via `client.stop_orders.post_stop_order()` (STOP_ORDER_TYPE_STOP_LOSS)
- Take-profit: real stop order via `client.stop_orders.post_stop_order()` (STOP_ORDER_TYPE_TAKE_PROFIT)
- Timeout: bot cancels all 3 orders and exits at market
- SL/TP are placed on the exchange immediately after entry — position is protected even if bot crashes

## Project structure
```
TraderBot/
  main.py                      # main polling loop (60s interval)
  backtest.py                  # walk-forward backtest
  bot/
    broker/tinkoff.py          # TinkoffBroker — all API calls
    data/storage.py            # SQLite + resample()
    strategies/
      base.py                  # Signal enum, BaseStrategy
      ict.py                   # ICTStrategy (the actual logic)
    execution/manager.py       # ExecutionManager — tracks positions, places SL/TP
    risk/manager.py            # RiskManager — 1% position sizing
    logs/journal.py            # TradeJournal — logs to trades.csv
  tests/
    test_tinkoff_broker.py     # 14 tests, all passing
```
