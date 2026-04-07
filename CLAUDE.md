# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**TraderBot** — multi-tenant autonomous trading bot for MOEX (Moscow Stock Exchange) via T-Bank Invest API. Operates as a SaaS: clients pay a subscription, register via Telegram, provide their T-Bank token, and the bot trades on all client accounts simultaneously using the same ICT strategy and risk parameters.

- **Language:** Python 3.12 (not 3.13+ — breaks T-Bank gRPC SDK)
- **Architecture:** Multi-tenant; one `ExecutionManager` per client, signals computed once per ticker cycle
- **Strategy:** ICT System Variant A (1H structure sweep, configurable sweep length and RR)
- **Config:** YAML-based with env-var token resolution; per-ticker strategy assignment
- **State:** SQLite (`data/traderbot.db`) — positions, trades, clients, payments
- **Notifications:** Telegram bot with full admin panel and client self-service commands
- **Payments:** Manual stub (`ManualProvider`); real provider integration ready via `PaymentProvider` Protocol

**Reference:** Full architecture, API contracts, and data structures in `DESIGN.md`.

## Setup & Dependencies

```bash
# Create virtual environment (Python 3.12 required)
py -3.12 -m venv venv
venv\Scripts\activate

# Install dependencies
pip install -r traderbot/requirements.txt
```

**Key SDK:** `t-tech-investments` (official T-Bank SDK)
- Imports: `from t_tech.invest import ...`
- NOT `tinkoff-investments` (old) or `tinkoff-invest` (unofficial REST wrapper)
- Installed from: `https://opensource.tbank.ru/api/v4/projects/238/packages/pypi/simple`

**Environment Variables:** Create `traderbot/.env` (template: `traderbot/.env.example`)
```
TBANK_MARKET_TOKEN=your_market_data_token
TBANK_ADMIN_TOKEN_1=your_admin_trading_token
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ADMIN_CHAT_ID=your_chat_id
```

## Development Commands

- **Live Trading:** `py -3.12 -m traderbot.main` (or double-click `start_bot.bat`)
- **Backtest:** `py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml --days 30` (or `start_backtest.bat`)
- **Check connectivity:** `py -3.12 -m traderbot.check` (or `start_check.bat`)

All commands must be run from the project root (`Tradebot-development/`), not from inside `traderbot/`.

## Project Structure

```
traderbot/
├── main.py                  # Multi-tenant live trading entry point
├── config.py                # Config loading (YAML + env vars)
├── config.yaml              # Tickers, strategies, risk params, admin tokens
├── types.py                 # Shared types: Signal, Setup, Position, TradeRecord
│
├── broker/
│   ├── tbank.py             # T-Bank API wrapper (only module talking to API)
│   ├── factory.py           # broker_from_client() — creates TBankBroker per client
│   └── pool.py              # BrokerPool — in-memory registry of active brokers
│
├── clients/
│   ├── db.py                # Database — SQLite init, WAL mode, thread-safe lock
│   ├── models.py            # Client dataclass, ClientStatus/ClientRole enums
│   └── registry.py          # ClientRegistry — CRUD over SQLite
│
├── data/
│   └── feed.py              # Candle loading + resampling by timeframe
│
├── strategies/
│   ├── base.py              # BaseStrategy (abstract)
│   ├── registry.py          # Strategy registry (name → class)
│   ├── ict.py               # ICT original (15-candle sweep)
│   └── ict_v2_*.py          # ICT V2 variants
│
├── risk/
│   └── manager.py           # Position sizing (per-client balance)
│
├── execution/
│   └── manager.py           # Order placement, position tracking, recovery
│                            # client_id injected in constructor; core logic unchanged
│
├── journal/
│   ├── writer.py            # CSV TradeJournal (legacy, admin-only)
│   ├── sqlite_writer.py     # SqliteTradeJournal — writes to trades table
│   └── multi_writer.py      # MultiTradeJournal + ClientJournalView adapter
│
├── notifications/
│   ├── telegram.py          # TelegramNotifier: send_to_client/send_admin/send_to_all_active
│   ├── bot.py               # TelegramBot — polling daemon + handler registration
│   ├── fsm.py               # In-memory FSM (onboarding + revoke dialog states)
│   └── handlers/
│       ├── client.py        # /start /help /pay /setup /status /pause /resume
│       ├── onboarding.py    # FSM: email → token → validate → active
│       └── admin.py         # /admin /clients /grant /revoke /broadcast /pnl_all ...
│
├── payments/
│   ├── provider.py          # PaymentProvider Protocol
│   └── manual.py            # ManualProvider stub (admin activates via /grant)
│
├── state/
│   ├── sqlite_store.py      # SqliteStateStore — positions/sl counters per client_id
│   └── client_view.py       # ClientStateView — injects client_id, old StateStore API
│
├── backtest/                # Backtest engine (single-account, unchanged)
│
├── data/traderbot.db        # SQLite database (created on first run)
├── logs/                    # Runtime logs
├── journal/                 # Admin CSV trades
└── state/                   # Legacy JSON (migrated → .migrated on first run)
```

## Key Architectural Principles

1. **One ExecutionManager per client.** Core trading logic (`open_position`, `update`, `recover`) is untouched. `client_id` is injected in the constructor and flows through `ClientStateView` and `ClientJournalView` adapters.

2. **Signals computed once per cycle.** `strategy.find_setup(candles)` is called once per ticker per main loop iteration. The result is distributed to all active `ExecutionManager` instances.

3. **Market data via dedicated token.** `TBANK_MARKET_TOKEN` fetches candles. Admin/client trading tokens are separate.

4. **Two client classes:**
   - **Admin** (`role='admin'`): defined in `config.yaml → admin.tokens`, upserted on every startup, no subscription required.
   - **Subscriber** (`role='subscriber'`): registers via `/start`, pays via `/pay`, activates via `/grant` + onboarding FSM.

5. **Token security:** Tokens are stored in plaintext in SQLite (filesystem-level security). In Telegram, the token message is deleted immediately (`bot.delete_message`). Tokens never appear in logs (masked as `***{last4}`).

6. **Client isolation:** Each client error increments `consecutive_errors`. After 5 consecutive errors, client is auto-paused and removed from `execs`. Other clients continue unaffected.

7. **Broker abstraction:** `broker/tbank.py` is the only module aware of T-Bank API.

## Multi-Tenant Data Flow

```
config.yaml + .env
    ↓
bootstrap_admin_clients()   # upsert admin records in DB
migrate_legacy_state()      # one-time: positions.json → SQLite
    ↓
md_broker = TBankBroker(TBANK_MARKET_TOKEN)   # market data only
    ↓
sync_execs() every 60s:
    for each ACTIVE client in registry:
        broker = broker_from_client(client)
        em = ExecutionManager(client_id=..., state=ClientStateView(...), ...)
        execs[client.id] = em
    ↓
main loop per ticker:
    candles = feed.get_candles(figi)   ← ONE call per ticker
    shared_setup = strategy.find_setup(candles)
    for client_id, em in execs:
        if em.has_position(figi): em.update(...)
        elif shared_setup: em.open_position(...)
```

## Telegram Bot — Commands Summary

### Client commands
| Command | Action |
|---|---|
| `/start` | Register (status: pending_payment) |
| `/pay` | Show payment instructions (ManualProvider) |
| `/setup` | Start onboarding FSM (requires pending_email status) |
| `/status` | Open positions + last 5 trades |
| `/pause` | Pause trading (status: paused, removed from execs) |
| `/resume` | Resume trading (sync_execs picks up within 60s) |
| `/help` | Command list based on current status |

### Admin commands
| Command | Action |
|---|---|
| `/grant <chat_id> <days>` | Activate subscription → pending_email |
| `/revoke <chat_id>` | Interactive dialog: close positions / wait SL-TP / cancel |
| `/clients` | List all clients |
| `/client <id>` | Client details + positions + last trades |
| `/pause_client <chat_id>` | Pause client |
| `/resume_client <chat_id>` | Resume client |
| `/broadcast <text>` | Send to all ACTIVE clients |
| `/pnl_all` | P&L summary across all clients |
| `/balance_all` | Live balances from broker |
| `/admin` | Overview stats |
| `/reload_clients` | Force sync_execs immediately |

## Common Implementation Tasks

### Adding a New Strategy

1. Create `strategies/my_strategy.py` inheriting from `BaseStrategy`
2. Implement `find_setup(candles) -> Setup | None`
3. Register in `strategies/registry.py`
4. Add to `config.yaml` under `tickers:` with `strategy: "my_strategy"`

### Adding a New Admin Command

1. Add handler function in `notifications/handlers/admin.py`
2. Use `@require_admin(bot, registry)` decorator pattern (see existing handlers)
3. Register the handler inside the `register()` function

### Integrating a Real Payment Provider

1. Implement `PaymentProvider` Protocol in `payments/your_provider.py`
2. Update `config.yaml → subscription.provider` and add required env vars
3. Instantiate in `main.py` (replace `ManualProvider`)

### Backtesting

- Run: `py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml`
- Single-account, uses same `strategies/` and `risk/` as live
- Does NOT use SQLite or client registry

## Configuration

### `config.yaml` structure

```yaml
database:
  path: "data/traderbot.db"

market_data:
  token_env: "TBANK_MARKET_TOKEN"
  app_name: "TraderBot-MD"

admin:
  chat_ids_env: "TELEGRAM_ADMIN_CHAT_ID"
  tokens:
    - token_env: "TBANK_ADMIN_TOKEN_1"
      name: "primary"
      chat_id_env: "TELEGRAM_ADMIN_CHAT_ID"

subscription:
  price_rub: 0
  period_days: 30
  provider: "manual"

telegram:
  token_env: "TELEGRAM_BOT_TOKEN"

risk:
  risk_pct: 0.05
  max_position_pct: 0.40
  max_consecutive_sl: 3

tickers:
  SBER:
    figi: "BBG004730N88"
    strategy: "ict_v2_sw10_rr2"

trading:
  poll_interval_sec: 60
  max_candles_timeout: 48
  commission_pct: 0.0004
```

## Notes

- Always run from project root (`Tradebot-development/`), not from `traderbot/`. Relative imports break otherwise.
- SQLite database is created automatically on first run at `traderbot/data/traderbot.db`.
- Legacy `state/positions.json` and `state/telegram_subscribers.json` are migrated to SQLite on first run (renamed to `.migrated`).
- Commissions are factored as flat %. Adjust `commission_pct` in config if T-Bank rates change.
- Position timeout is in 30m candles, not wall-clock time (`max_candles_timeout`).
- `main_legacy.py` is the pre-multi-tenant entry point, kept for reference. Do not use for live trading.
