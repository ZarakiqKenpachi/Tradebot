# TraderBot — Проектный документ

Полное описание архитектуры, модулей и интерфейсов.
Источник истины для понимания системы.

---

## 1. Цель проекта

**TraderBot** — мультитенантный автономный торговый бот для MOEX (Московская биржа) через T-Bank Invest API.

Модель работы — **SaaS**:
- Клиент платит подписку → регистрируется через Telegram → предоставляет токен T-Bank.
- Бот торгует на счёте клиента одновременно с другими клиентами по той же стратегии.
- Один экземпляр процесса обслуживает произвольное число клиентов.

**Ключевые свойства:**
- Рыночные данные загружаются **один раз** через выделенный `TBANK_MARKET_TOKEN`.
- Торговый сигнал вычисляется **один раз** на тикер за цикл, затем раздаётся всем клиентам.
- Ордера размещаются **per-client** через их собственные торговые токены.
- При ошибке одного клиента остальные продолжают работу.
- Полная Telegram-админка для управления клиентами.

---

## 2. Архитектура

```
config.yaml + .env
       │
       ▼
  bootstrap_admin_clients()          ← upsert admin из config в SQLite
  migrate_legacy_state()             ← однократно: positions.json → SQLite
       │
       ▼
  md_broker = TBankBroker(TBANK_MARKET_TOKEN)   ← только для свечей
  feed = DataFeed(md_broker)
       │
       ▼
  sync_execs() каждые 60 сек:
    для каждого active-клиента в registry:
      broker = broker_from_client(client)
      em = ExecutionManager(client_id=..., state=ClientStateView(...), ...)
      execs[client.id] = em
       │
       ▼
  Основной цикл (per ticker):
    candles = feed.get_candles(figi)     ← ОДИН вызов
    setup   = strategy.find_setup()     ← ОДИН раз
    для каждого (client_id, em) в execs:
      if em.has_position(figi): em.update(...)
      elif setup:               em.open_position(...)
```

### Изоляция клиентов

Каждый клиент получает:
- Свой `TBankBroker` (свой токен → свой счёт)
- `ClientStateView(sqlite_store, client_id)` — изолированное хранилище позиций
- `ClientJournalView(multi_journal, client_id)` — изолированный журнал сделок
- `ExecutionManager(client_id=...)` — всё ядро исполнения без изменений

Адаптер `ClientStateView` подставляет `client_id` в каждый вызов `SqliteStateStore` так, что `ExecutionManager` не знает о мультитенантности.

---

## 3. Структура проекта

```
traderbot/
├── main.py                  # Точка входа — multi-tenant trading loop
├── config.py                # Загрузка конфига (YAML + env)
├── config.yaml              # Тикеры, стратегии, admin-токены, риск
├── types.py                 # Общие типы: Signal, Setup, Position, TradeRecord
│
├── broker/
│   ├── tbank.py             # T-Bank API wrapper (единственный модуль, знающий API)
│   ├── factory.py           # broker_from_client() → (TBankBroker, account_id)
│   └── pool.py              # BrokerPool — in-memory реестр активных брокеров
│
├── clients/
│   ├── db.py                # Database — SQLite, WAL, threading.RLock
│   ├── models.py            # Client dataclass, ClientStatus/ClientRole enum
│   └── registry.py          # ClientRegistry — CRUD поверх SQLite
│
├── data/
│   └── feed.py              # Загрузка и ресемплинг свечей
│
├── strategies/
│   ├── base.py              # BaseStrategy (абстрактный)
│   ├── registry.py          # Реестр: имя → класс
│   ├── ict.py               # ICT оригинальный (sweep=15)
│   └── ict_v2_*.py          # ICT V2 варианты
│
├── risk/
│   └── manager.py           # RiskManager — размер позиции per-client баланс
│
├── execution/
│   └── manager.py           # Ядро исполнения — open/update/recover
│                            # client_id в конструкторе; логика не менялась
│
├── journal/
│   ├── writer.py            # TradeJournal CSV (admin-only legacy writer)
│   ├── sqlite_writer.py     # SqliteTradeJournal → таблица trades
│   └── multi_writer.py      # MultiTradeJournal + ClientJournalView adapter
│
├── notifications/
│   ├── telegram.py          # TelegramNotifier: to_client / admin / all_active
│   ├── bot.py               # TelegramBot — polling daemon + регистрация хендлеров
│   ├── fsm.py               # In-memory FSM (онбординг + revoke-диалог)
│   └── handlers/
│       ├── client.py        # /start /help /pay /setup /status /pause /resume
│       ├── onboarding.py    # FSM: email → токен → валидация → active
│       └── admin.py         # /admin /clients /grant /revoke /broadcast /pnl_all ...
│
├── payments/
│   ├── provider.py          # PaymentProvider Protocol
│   └── manual.py            # ManualProvider: инструкция + активация через /grant
│
├── state/
│   ├── sqlite_store.py      # SqliteStateStore: позиции/SL-счётчики per client_id
│   └── client_view.py       # ClientStateView: прокси со старым интерфейсом StateStore
│
└── backtest/                # Движок бэктеста (однотенантный, не трогался)
```

---

## 4. База данных (SQLite)

Файл: `traderbot/data/traderbot.db`, создаётся автоматически.

### Таблицы

```sql
-- Версия схемы (для будущих миграций)
schema_version (version INTEGER)

-- Клиенты: и admin, и subscriber
clients (
    id, tg_chat_id UNIQUE, role, status,
    email, account_name,
    tbank_token, tbank_account_id,
    paid_until,                  -- NULL для admin
    consecutive_errors,
    created_at, updated_at
)

-- Открытые позиции (per client)
positions (
    id, client_id FK, figi, ticker, direction,
    entry_price, stop_price, target_price,
    qty, lot_size, entry_time, entry_reason,
    entry_order_id, sl_order_id, tp_order_id,
    status, candles_held, pending_candles, last_candle_time
    UNIQUE(client_id, figi)
)

-- SL-счётчики per (client, ticker)
consecutive_sl (client_id FK, ticker, count, last_sl_date)

-- Журнал закрытых сделок
trades (
    id, client_id FK, ticker, figi, direction,
    entry_price, exit_price, stop_price, target_price,
    qty, pnl, commission,
    entry_time, exit_time, entry_reason, exit_reason, candles_held
)

-- Платежи
payments (
    id, client_id FK, provider, amount_rub, status,
    external_id, period_days, created_at, paid_at
)
```

### Статусная машина клиента

```
NEW ──/start──► pending_payment
pending_payment ──/grant──► pending_email
pending_email   ──email──► pending_token
pending_token   ──токен+ОК──► active
active ──/pause──► paused ──/resume──► active
active ──paid_until<now──► expired          (авто в sync_execs)
active ──/revoke──► revoked (+ диалог)
```

---

## 5. Два класса клиентов

| Класс | role | Токен | paid_until | Описание |
|---|---|---|---|---|
| **Admin** | `admin` | из `config.yaml → admin.tokens` | NULL | Self-run, бесплатно, upsert при каждом старте |
| **Subscriber** | `subscriber` | из Telegram-онбординга | ISO8601 | Платная подписка, активация через `/grant` |

---

## 6. Telegram-бот

### Клиентские команды

| Команда | Статус | Действие |
|---|---|---|
| `/start` | any | Регистрация (pending_payment) или приветствие |
| `/pay` | any | Инструкция по оплате |
| `/setup` | pending_email | Запуск FSM онбординга |
| `/status` | any | Позиции + последние 5 сделок |
| `/pause` | active | Остановить торговлю |
| `/resume` | paused | Возобновить торговлю |
| `/help` | any | Список команд по статусу |

### Онбординг FSM

```
PENDING_EMAIL
  └─ ввод email → regex → save → status=pending_token → инструкция токена

PENDING_TOKEN
  └─ ввод токена →
       1. delete_message (немедленно)
       2. TBankBroker.get_account_id() + get_portfolio_balance()
       3a. ОК → save token+account_id, status=active, уведомить admin
       3b. Fail → остаться в PENDING_TOKEN, показать ошибку
```

Токены **никогда не попадают в логи**: маска `***{последние 4 символа}`.

### Админские команды

| Команда | Действие |
|---|---|
| `/admin` | Сводка: клиентов по статусам, P&L за сегодня |
| `/clients` | Список всех клиентов |
| `/client <id>` | Детали: email, баланс, позиции, последние 10 сделок |
| `/grant <chat_id> <days>` | Продлить подписку → pending_email, уведомить клиента |
| `/revoke <chat_id>` | Диалог с inline-кнопками (см. ниже) |
| `/pause_client <chat_id>` | Принудительная пауза |
| `/resume_client <chat_id>` | Возобновить |
| `/broadcast <текст>` | Разослать всем active |
| `/pnl_all` | Суммарный P&L по всем клиентам |
| `/balance_all` | Балансы всех активных через broker API |
| `/reload_clients` | Форсировать sync_execs |

### `/revoke` диалог

```
[Закрыть все по рынку]   → em._close_position per figi, status=revoked, token=NULL
[Оставить до SL/TP]      → em._revoked=True, status=revoked, новых позиций нет
[Отменить revoke]        → ничего не делаем
```

---

## 7. Конфигурация

### `config.yaml`

```yaml
database:
  path: "data/traderbot.db"

market_data:
  token_env: "TBANK_MARKET_TOKEN"
  app_name: "TraderBot-MD"

admin:
  chat_ids_env: "TELEGRAM_ADMIN_CHAT_ID"   # CSV нескольких admin chat_id
  tokens:
    - token_env: "TBANK_ADMIN_TOKEN_1"
      name: "primary"
      chat_id_env: "TELEGRAM_ADMIN_CHAT_ID"

subscription:
  price_rub: 0           # 0 = бесплатно (задать перед продакшеном)
  period_days: 30
  provider: "manual"

telegram:
  token_env: "TELEGRAM_BOT_TOKEN"

risk:
  risk_pct: 0.05          # 5% риска на сделку
  max_position_pct: 0.40  # макс. 40% баланса на позицию
  max_consecutive_sl: 3   # блокировка тикера после 3 SL подряд

tickers:
  SBER:
    figi: "BBG004730N88"
    strategy: "ict_v2_sw10_rr2"

trading:
  poll_interval_sec: 60
  max_candles_timeout: 48   # таймаут в 30m-свечах
  commission_pct: 0.0004
```

### `.env`

```
TBANK_MARKET_TOKEN=...       # токен для свечей
TBANK_ADMIN_TOKEN_1=...      # торговый токен администратора
TELEGRAM_BOT_TOKEN=...
TELEGRAM_ADMIN_CHAT_ID=...   # chat_id, CSV для нескольких
```

---

## 8. Платёжная система

Интерфейс `PaymentProvider` (Protocol):

```python
def create_invoice(client_id, amount_rub, period_days) -> str  # текст инструкции
def check_status(payment_id) -> str                            # 'pending'|'paid'|'failed'
```

Текущая реализация — `ManualProvider`:
- `create_invoice` → показывает клиенту реквизиты и сохраняет `payments` запись со статусом `pending`
- Активация — через `/grant` от администратора (создаёт `payments` запись `paid`, переводит в `pending_email`)

Для подключения реального провайдера: реализовать Protocol в `payments/your_provider.py`, заменить в `main.py`.

---

## 9. Стратегии

Все стратегии наследуют `BaseStrategy`:
```python
class BaseStrategy:
    required_timeframes: list[str]
    def find_setup(self, candles: dict[str, pd.DataFrame]) -> Setup | None: ...
```

| Стратегия | Sweep | RR | Описание |
|---|---|---|---|
| `ict` | 15 | 1:2 | ICT System Variant A оригинальный |
| `ict_v2_sw4_rr2` | 4 | 1:2 | V2, короткий sweep |
| `ict_v2_sw4_rr35` | 4 | 1:3.5 | V2, короткий sweep, высокий RR |
| `ict_v2_sw10_rr2` | 10 | 1:2 | V2, средний sweep |
| `ict_v2_tester` | — | — | Параметры в файле, для экспериментов |

Добавление стратегии: создать файл → наследовать `BaseStrategy` → зарегистрировать в `strategies/registry.py` → указать в `config.yaml`.

---

## 10. Безопасность

- **Токены в БД** — открытый текст, защита на уровне файловой системы.
- **Токен в Telegram** — сообщение удаляется немедленно (`bot.delete_message` до любой обработки).
- **Логи** — токен маскируется как `***{last4}`, никогда не пишется полностью.
- **Thread safety** — все write-операции в SQLite под `threading.RLock`.
- **Изоляция ошибок** — после 5 последовательных ошибок клиент уходит в `paused`, остальные не затронуты.

---

## 11. Бэктест

Однотенантный, не использует клиентский реестр и SQLite.

```bash
py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml --days 30
py -3.12 -m traderbot.backtest.compare --config traderbot/config.yaml --days 90
```

Результаты — в `traderbot/backtest/results/`.
