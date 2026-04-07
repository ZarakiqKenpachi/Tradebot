# TraderBot

Мультитенантный автономный торговый бот для MOEX (Московская биржа) через T-Bank Invest API.

**Модель:** SaaS — клиент платит подписку, регистрируется через Telegram, предоставляет торговый токен T-Bank, бот торгует на его счёте по той же ICT-стратегии что и у администратора.

**Стратегия:** ICT System — анализ структуры рынка на 1H таймфрейме.

---

## Требования

- **Python 3.12** (строго — Python 3.13+ ломает gRPC SDK от T-Bank)
- Аккаунт T-Bank Invest с API токеном
- Telegram бот (от @BotFather)

---

## Быстрый старт (Windows)

```
start_check.bat    — проверить подключение перед запуском
start_bot.bat      — запустить торгового бота
start_backtest.bat — запустить бэктест
```

Все `.bat` файлы находятся в корне проекта. Двойной клик — и готово.

---

## Установка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/ZarakiqKenpachi/Tradebot.git
cd Tradebot
git checkout development
```

### 2. Установить зависимости

> **Важно:** все команды запускать из корневой папки (`Tradebot-development/`), не заходя в `traderbot/`.

```bash
py -3.12 -m venv venv
venv\Scripts\activate
pip install -r traderbot/requirements.txt
```

> SDK от T-Bank (`t-tech-investments`) устанавливается автоматически с их PyPI-сервера.

### 3. Настроить переменные окружения

Скопировать шаблон:

```bash
copy .env.example .env
```

Открыть `.env` и заполнить:

```env
# Токен для загрузки свечей (market data)
TBANK_MARKET_TOKEN=your_market_data_token

# Ваш торговый токен (self-run администратора)
TBANK_ADMIN_TOKEN_1=your_admin_trading_token

# Telegram
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_ADMIN_CHAT_ID=123456789
```

**Где взять токены:**
- **TBANK_MARKET_TOKEN / TBANK_ADMIN_TOKEN_1** — T-Bank Invest → Настройки → Инвестиции → API → Создать токен
- **TELEGRAM_BOT_TOKEN** — создать бота через [@BotFather](https://t.me/BotFather)
- **TELEGRAM_ADMIN_CHAT_ID** — написать что угодно своему боту, затем узнать ID через [@userinfobot](https://t.me/userinfobot)

---

## Проверка перед запуском

```bash
start_check.bat
```

или

```bash
py -3.12 -m traderbot.check
```

Проверяет токены, аккаунт, баланс, рыночные данные и Telegram. Ничего не торгует.

---

## Запуск бота

```bash
start_bot.bat
```

или

```bash
py -3.12 -m traderbot.main
```

При запуске бот:
1. Подключается к T-Bank с вашим токеном (admin self-run)
2. Создаёт/мигрирует БД (`data/traderbot.db`)
3. Запускает Telegram-бот для управления клиентами
4. Начинает торговать по расписанию

Остановка — `Ctrl+C`. Открытые позиции и SL/TP остаются на бирже. При следующем запуске бот восстановит их из БД.

---

## Подключение клиентов (Telegram)

### Со стороны клиента

1. Написать боту `/start` → зарегистрироваться
2. `/pay` → получить инструкцию по оплате (или ссылку на оплату)
3. После подтверждения администратором: `/setup` → ввести email → ввести T-Bank токен
4. Бот подтверждает подключение и начинает торговать

### Со стороны администратора

```
/grant <chat_id> <дней>    — подтвердить оплату, выдать подписку
/clients                   — список всех клиентов
/client <id>               — детали: позиции, P&L, статус
/revoke <chat_id>          — отозвать доступ (с диалогом про открытые позиции)
/pnl_all                   — суммарный P&L по всем клиентам
/balance_all               — балансы всех активных клиентов
/broadcast <текст>         — сообщение всем активным клиентам
/reload_clients            — принудительно обновить реестр
```

### Команды клиента

```
/status   — открытые позиции + последние 5 сделок
/pause    — приостановить торговлю
/resume   — возобновить торговлю
/help     — список команд
```

---

## Бэктест

### Базовый запуск

```bash
start_backtest.bat
```

или

```bash
py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml --days 30
```

### Флаги

| Флаг | По умолчанию | Описание |
|---|---|---|
| `--days` | из конфига | Глубина истории |
| `--tickers` | все | Тикеры через запятую: `SBER,GAZP` |
| `--config` | `config.yaml` | Путь к конфигу или `live`/`test` |

### Сравнение стратегий

```bash
py -3.12 -m traderbot.backtest.compare --config traderbot/config.yaml --days 90
```

---

## Структура проекта

```
Tradebot-development/
├── start_bot.bat            # Запуск торгового бота
├── start_backtest.bat       # Запуск бэктеста
├── start_check.bat          # Диагностика подключения
│
└── traderbot/
    ├── main.py              # Multi-tenant точка входа
    ├── config.yaml          # Тикеры, стратегии, токены
    ├── .env                 # Ваши токены (не в git)
    ├── .env.example         # Шаблон
    │
    ├── clients/             # SQLite реестр клиентов
    ├── broker/              # T-Bank API + BrokerPool
    ├── state/               # Хранение позиций (SQLite)
    ├── journal/             # Журнал сделок (SQLite + CSV)
    ├── execution/           # Исполнение ордеров
    ├── strategies/          # ICT стратегии
    ├── risk/                # Риск-менеджмент
    ├── notifications/       # Telegram бот + хендлеры
    ├── payments/            # Провайдер оплаты
    ├── data/                # feed.py + traderbot.db
    └── backtest/            # Движок бэктеста
```

---

## Настройка инструментов

Редактировать `traderbot/config.yaml`:

```yaml
tickers:
  SBER:
    figi: "BBG004730N88"
    strategy: "ict_v2_sw10_rr2"
  # Добавить или убрать тикеры здесь
```

Доступные стратегии:

| Стратегия | Sweep | RR | Описание |
|---|---|---|---|
| `ict` | 15 | 1:2 | Оригинальная ICT System Variant A |
| `ict_v2_sw4_rr2` | 4 | 1:2 | V2 — короткий sweep, умеренный RR |
| `ict_v2_sw4_rr35` | 4 | 1:3.5 | V2 — короткий sweep, высокий RR |
| `ict_v2_sw10_rr2` | 10 | 1:2 | V2 — средний sweep, умеренный RR |
| `ict_v2_tester` | ? | ? | Тестовая — параметры в файле |

### Основные параметры

| Параметр | Секция | Описание |
|---|---|---|
| `risk_pct` | `risk` | Риск на сделку (0.05 = 5%) |
| `max_position_pct` | `risk` | Макс. размер позиции (0.40 = 40%) |
| `max_consecutive_sl` | `risk` | Блокировка тикера после N SL подряд |
| `poll_interval_sec` | `trading` | Как часто проверять рынок (секунды) |
| `max_candles_timeout` | `trading` | Таймаут позиции в 30m-свечах |
| `commission_pct` | `trading` | Комиссия брокера (0.0004 = 0.04%) |
| `price_rub` | `subscription` | Стоимость подписки в рублях |
| `period_days` | `subscription` | Срок подписки в днях |

---

## Как работает исполнение ордеров

Позиция открывается в два этапа:

1. **Лимитная заявка выставлена** → уведомление в Telegram. SL и TP ещё не выставлены.
2. **Заявка исполнена** → SL и TP выставляются на биржу, позиция активна → уведомление.

Лимитная заявка, не исполненная за 20 свечей (30m), отменяется автоматически.

Таймаут открытой позиции считается в **30-минутных свечах** (`max_candles_timeout`).

---

## Лучший результат бэктеста

**SBER, 30 дней:** 66.7% winrate, +17.96% к балансу (стратегия `ict`, 15-свечной lookback).
