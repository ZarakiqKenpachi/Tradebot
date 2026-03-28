# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TraderBot** — an autonomous trading bot for MOEX (Moscow Stock Exchange) via T-Bank Invest API.

- **Language:** Python 3.12 (not 3.14 — breaks T-Bank gRPC SDK)
- **Architecture:** Modular by responsibility (broker, data, strategies, risk, execution, state, notifications, journal, backtest)
- **Strategy:** ICT System Variant A (15-candle 1H structure sweep)
- **Config:** YAML-based, supports multiple tickers with independent strategies
- **State:** Persistent JSON storage for recovery on crash
- **Notifications:** Telegram alerts
- **Testing:** Backtesting engine using same logic as live trading

**Reference:** Full architecture, API contracts, and data structures in `DESIGN.md` — this is the source of truth for implementation.

## Setup & Dependencies

```bash
# Create virtual environment (Python 3.12 required)
python -3.12 -m venv venv
source venv/Scripts/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies (from requirements.txt)
pip install -r requirements.txt
```

**Key SDK:** `t-tech-investments` (official T-Bank SDK)
- Imports: `from t_tech.invest import ...`
- NOT `tinkoff-investments` (old) or `tinkoff-invest` (unofficial REST wrapper)
- Installed from: `https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple`

**Environment Variables:** Create `.env` file (template in `.env.example`)
```
TBANK_TOKEN=your_sandbox_token
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

## Development Commands

*(These commands will be added once codebase is initialized)*

- **Live Trading:** `py -3.12 main.py`
- **Backtest:** `py -3.12 backtest/run.py --config config.yaml --days 30`
- **Linting:** `pylint traderbot/` *(if/when configured)*
- **Tests:** `pytest tests/` *(if/when configured)*

## Project Structure

```
traderbot/
├── main.py                  # Live trading entry point
├── config.py                # Config loading & validation
├── config.yaml              # Instrument/strategy/parameter config
├── types.py                 # Shared types: Signal, Setup, Position, TradeRecord
│
├── broker/
│   └── tbank.py             # T-Bank API wrapper (only module talking to API)
│
├── data/
│   └── feed.py              # Candle loading + resampling by timeframe
│
├── strategies/
│   ├── base.py              # BaseStrategy (abstract, all strategies inherit)
│   ├── registry.py          # Strategy registry (name → class)
│   └── ict.py               # ICT strategy implementation
│
├── risk/
│   └── manager.py           # Position sizing, balance limits
│
├── execution/
│   └── manager.py           # Order placement, position tracking, recovery
│
├── journal/
│   └── writer.py            # Trade logging (CSV format)
│
├── notifications/
│   └── telegram.py          # Telegram alerts
│
├── state/
│   └── store.py             # Persistent position storage (JSON)
│
├── backtest/
│   ├── run.py               # Backtest CLI entry point
│   ├── engine.py            # Simulation engine
│   └── report.py            # Results & statistics
│
├── logs/                    # Runtime logs (bot.log)
├── journal/                 # Trade CSVs (trades.csv)
├── state/                   # Position JSON (positions.json)
└── backtest/results/        # Backtest CSVs
```

## Key Architectural Principles

1. **Modular by responsibility:** Each folder = one module with one concern. Changes to `strategies/ict.py` should not require touching other modules.

2. **Config-driven:** `config.yaml` determines which tickers trade which strategies. No hardcoding.

3. **Shared types in `types.py`:** All inter-module communication uses `Signal`, `Setup`, `Position`, `TradeRecord`.

4. **Broker abstraction:** `broker/tbank.py` is the only module aware of T-Bank API. If SDK changes, only this module needs updates.

5. **Data flow:**
   - **Live:** `data/feed.py` → `strategies/*.py` (generates `Setup`) → `execution/` (places orders) → `state/` (stores) → `journal/` (logs)
   - **Backtest:** `data/feed.py` → `backtest/engine.py` (simulates execution) → `backtest/report.py` (analysis)

6. **Recovery:** Open positions are stored in `state/positions.json`. On restart, `execution/` rehydrates positions and recreates missing SL/TP orders.

7. **Same logic everywhere:** Backtest uses same `strategies/`, `risk/` modules as live. No separate backtesting-only logic.

## Common Implementation Tasks

### Adding a New Strategy

1. Create `strategies/my_strategy.py` inheriting from `BaseStrategy`
2. Implement `analyze(candles: pd.DataFrame) -> Setup | None` returning trade setup or None
3. Register in `strategies/registry.py`
4. Add to `config.yaml` under `tickers:` with `strategy: "my_strategy"`

### Adding Notifications

- Edit `notifications/telegram.py` to add new alert types
- Call from `execution/` or `main.py` as trades execute

### Backtesting

- Run: `py -3.12 backtest/run.py --config config.yaml`
- Results: CSV files in `backtest/results/`
- Uses `backtest/engine.py` to simulate, same `strategies/` and `risk/` as live

### Sandbox Testing

- Token in `.env` uses sandbox mode (set in `config.yaml`)
- Sandbox account `8c77a9bb-b8f5-4fd8-b909-005cca158d8b` funded with 100k RUB
- Safe for testing order placement, recovery logic, etc.

## Testing Strategy

- **Backtest** to validate strategy logic without real money
- **Sandbox** to test broker integration, order execution, recovery
- **Paper live** (optional) to test in real market without risk
- Best backtest result so far: 66.7% win rate, +17.96% over 30 days on SBER with 15-candle lookback

## Configuration Details

Refer to `config.yaml` structure in `DESIGN.md` § 3 for all parameters:
- **broker:** Token source, sandbox flag, app name
- **risk:** Risk % per trade, max position %
- **tickers:** FIGI, strategy assignment
- **notifications:** Telegram config
- **trading:** Poll interval, position timeout (candles), commission
- **backtest:** Initial balance, lookback days, output directory

## Notes

- Commissions are factored as flat %. Real T-Bank rates may vary by instrument — adjust `config.yaml` if needed.
- Timeframe is primarily 1H (specified in strategy), with 30m displacement for entries.
- Position timeout is measured in candles (not wall-clock time) — controlled by `trading.max_candles_timeout`.
