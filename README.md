# TraderBot

Автономный торговый бот для MOEX (Московская биржа) через T-Bank Invest API.

**Стратегия:** ICT System — анализ структуры рынка на 1H таймфрейме.
**Инструменты по умолчанию:** SBER, GAZP, GMKN, VTBR, ROSN, NVTK, TATN.

---

## Требования

- **Python 3.12** (строго — Python 3.13+ ломает gRPC SDK от T-Bank)
- Аккаунт T-Bank Invest с API токеном
- Telegram бот для уведомлений (опционально)

---

## Установка

### 1. Клонировать репозиторий

```bash
git clone https://github.com/ZarakiqKenpachi/Tradebot.git
cd Tradebot
git checkout development
```

### 2. Установить зависимости

> **Важно:** все команды запускать из корневой папки проекта (`Tradebot/`), **не заходя** в `traderbot/`. Иначе Python перепутает наш файл `types.py` со стандартным модулем и выдаст ошибку импорта.

**Вариант A — с виртуальным окружением (рекомендуется):**

```bash
python -3.12 -m venv venv
```

Активировать:
- **Windows:** `venv\Scripts\activate`
- **Linux/Mac:** `source venv/bin/activate`

```bash
pip install -r traderbot/requirements.txt
```

**Вариант Б — без виртуального окружения:**

```bash
py -3.12 -m pip install -r traderbot/requirements.txt
```

> SDK от T-Bank (`t-tech-investments`) устанавливается автоматически с их PyPI-сервера.

### 3. Настроить переменные окружения

Скопировать шаблон и заполнить:

```bash
cp .env.example .env
```

Открыть `.env` и вписать свои данные:

```env
TBANK_SANDBOX_TOKEN=your_sandbox_token_here
TBANK_LIVE_TOKEN=your_live_token_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
```

**Где взять токены:**
- **TBANK_SANDBOX_TOKEN** — личный кабинет T-Bank Invest → Настройки → API → Sandbox токен
- **TBANK_LIVE_TOKEN** — там же, токен для реальной торговли
- **TELEGRAM_BOT_TOKEN** — создать бота через [@BotFather](https://t.me/BotFather) в Telegram
- **TELEGRAM_CHAT_ID** — написать что угодно своему боту, затем узнать ID через [@userinfobot](https://t.me/userinfobot)

> Если Telegram недоступен (заблокирован провайдером) — нужен VPN. Бот продолжит работу даже без Telegram, просто без уведомлений.

Бот автоматически выбирает нужный токен по флагу `sandbox` в `config.yaml`.

---

## Проверка перед запуском

```bash
py -3.12 -m traderbot.check
```

Скрипт проверяет токен, аккаунт, баланс, рыночные данные и Telegram. Ничего не торгует.

---

## Запуск

### Sandbox (тестовый режим)

В `traderbot/config.yaml` установить `sandbox: true`, затем:

```bash
py -3.12 -m traderbot.main
```

Sandbox-аккаунт создаётся автоматически и пополняется 100 000 RUB.

### Live (реальная торговля)

1. Убедиться что `TBANK_LIVE_TOKEN` заполнен в `.env`
2. Пополнить счёт (минимум ~1 000 RUB — иначе не хватит на 1 лот)
3. В `traderbot/config.yaml` поменять `sandbox: false`
4. Запустить:

```bash
py -3.12 -m traderbot.main
```

Остановка — закрыть консоль или `Ctrl+C`. Открытые позиции и SL/TP остаются на бирже. При следующем запуске бот восстановит их из `state/positions.json`.

### Бэктест

#### Базовый запуск

```bash
py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml --days 30
```

#### Флаги

| Флаг | Тип | По умолчанию | Описание |
|---|---|---|---|
| `--config` | путь или шорткат | `config.yaml` | Путь к файлу конфига, или `live` / `test` |
| `--days` | число | из конфига | Глубина истории в днях |
| `--tickers` | строка | все из конфига | Тикеры через запятую: `SBER,GAZP` |

**Шорткаты `--config`:**

| Значение | Путь |
|---|---|
| `live` | `traderbot/config.yaml` |
| `test` | `traderbot/backtest/test_config.yaml` |

#### Примеры

```bash
# Живой конфиг, 30 дней
py -3.12 -m traderbot.backtest.run --config live --days 30

# Тестовый конфиг, только SBER и GAZP, 90 дней
py -3.12 -m traderbot.backtest.run --config test --days 90 --tickers SBER,GAZP

# Конфиг по умолчанию, глубина из config.yaml
py -3.12 -m traderbot.backtest.run
```

#### Результаты

В конце бэктест выводит сводку прямо в консоль:

```
============================================================
BACKTEST SUMMARY
============================================================
Начальный баланс:    100,000.00 RUB
Конечный баланс:     117,960.00 RUB
P&L:                 +17,960.00 RUB
Комиссия:              1,200.00 RUB
Доходность:              +17.96%
Всего сделок:                18
Побед:                       12
Поражений:                    6
Win rate:                 66.7%

Причины закрытия:
  stop_loss            4
  take_profit         10
  timeout              4

Тикер    Сделки    W    L    Win%          P&L
--------------------------------------------------
SBER          18   12    6   66.7%    +17,960.00
...
```

CSV-отчёты по каждому тикеру сохраняются в `traderbot/backtest/results/`:

```
backtest/results/
├── SBER_trades.csv
├── GAZP_trades.csv
└── ...
```

Колонки CSV:

| Колонка | Описание |
|---|---|
| `entry_time` | Время входа (ISO 8601) |
| `exit_time` | Время выхода |
| `direction` | `BUY` / `SELL` |
| `entry_price` | Цена входа |
| `exit_price` | Цена выхода |
| `stop_price` | Уровень стоп-лосса |
| `target_price` | Уровень тейк-профита |
| `qty` | Количество лотов |
| `pnl` | P&L сделки (с комиссией), RUB |
| `commission` | Комиссия за сделку, RUB |
| `exit_reason` | `take_profit` / `stop_loss` / `timeout` / `end_of_data` |
| `candles_held` | Сколько 30m-свечей держалась позиция |
| `entry_reason` | Описание сигнала (структурный свип, импульс, ретрейсмент) |
| `balance_after` | Баланс после закрытия сделки |

---

### Сравнение стратегий (compare)

`compare` прогоняет все стратегии по одним и тем же данным и выводит матрицу P&L, чтобы найти лучшую стратегию для каждого тикера.

```bash
py -3.12 -m traderbot.backtest.compare --config traderbot/config.yaml --days 90
```

| Флаг | Тип | По умолчанию | Описание |
|---|---|---|---|
| `--config` | путь или шорткат | `traderbot/config.yaml` | Путь к конфигу, или `live` / `test` |
| `--days` | число | 90 | Глубина истории |
| `--tickers` | строка | все из конфига | Тикеры через запятую |
| `--strategies` | строка | все варианты | Стратегии через запятую: `ict,ict_v2_sw4_rr2` |

```bash
# Все стратегии, живой конфиг, 90 дней
py -3.12 -m traderbot.backtest.compare --config live --days 90

# Только две стратегии, один тикер
py -3.12 -m traderbot.backtest.compare --config test --days 60 --tickers SBER --strategies ict,ict_v2_sw10_rr2
```

Вывод — три блока:

1. **Полная матрица** — P&L + winrate + кол-во сделок для каждой пары `(тикер × стратегия)`
2. **Лучшая стратегия по каждому тикеру** — P&L, TP/SL/TO, WR, profit factor
3. **Сравнение** — Best Mix vs каждая стратегия в едином портфеле

> Данные загружаются из API один раз — все стратегии гоняются по одному набору свечей.

---

## Как работает исполнение ордеров

Позиция открывается в два этапа:

1. **Лимитная заявка выставлена** → уведомление в Telegram. SL и TP ещё не выставлены.
2. **Лимитная заявка исполнена** → SL и TP выставляются на биржу, позиция считается открытой → уведомление в Telegram.

Если лимитная заявка не исполняется за **20 свечей по 30 минут** — отменяется автоматически.

Таймаут открытой позиции (`max_candles_timeout`) считается в **30-минутных свечах**.

---

## Telegram уведомления

| Событие | Сообщение |
|---|---|
| Запуск бота | `Бот запущен. Режим: live. Тикеров: 7` |
| Лимитная заявка выставлена | `📋 Лимитная заявка VTBR BUY ...` |
| Позиция открыта (лимитка исполнена) | `🟢 Позиция открыта VTBR BUY ...` |
| Позиция закрыта (SL/TP/таймаут) | `🔴 Закрыта позиция VTBR BUY ...` |
| Заявка отменена по таймауту | `❌ Лимитная заявка отменена ...` |
| Заявка отменена вручную через терминал | `❌ Лимитная заявка отменена через терминал ...` |
| Перезапуск с позициями | `Бот перезапущен. Активных: 1, ожидающих: 0` |
| Критическая ошибка | `Ошибка: ...` |

---

## Настройка инструментов

Редактируй `traderbot/config.yaml`:

```yaml
tickers:
  SBER:
    figi: "BBG004730N88"
    strategy: "ict_v2_sw10_rr2"
  # Добавь или убери тикеры здесь
```

Доступные стратегии:

| Стратегия | Sweep | RR | Описание |
|---|---|---|---|
| `ict` | 15 | 1:2 | Оригинальная ICT System Variant A |
| `ict_v2_sw4_rr2` | 4 | 1:2 | V2 — короткий sweep, умеренный RR |
| `ict_v2_sw4_rr35` | 4 | 1:3.5 | V2 — короткий sweep, высокий RR |
| `ict_v2_sw10_rr2` | 10 | 1:2 | V2 — средний sweep, умеренный RR |
| `ict_v2_tester` | ? | ? | Тестовая — параметры меняются в файле |

Параметры `ict_v2_tester` редактируются в `traderbot/strategies/ict_v2_tester.py` (константы наверху файла).

### Основные параметры конфига

| Параметр | Секция | Описание |
|---|---|---|
| `sandbox` | `broker` | `true` = тестовый режим, `false` = реальная торговля |
| `risk_pct` | `risk` | Риск на сделку (0.10 = 10% от баланса) |
| `max_position_pct` | `risk` | Макс. размер позиции на тикер (0.40 = 40% от депозита). Депозит берётся из API при старте. Лимит независимый — каждый тикер может занять до 40%, суммарно допускается плечо. |
| `max_consecutive_sl` | `risk` | Блокировка тикера после N стоп-лоссов подряд |
| `poll_interval_sec` | `trading` | Как часто проверять рынок (секунды) |
| `max_candles_timeout` | `trading` | Таймаут позиции в 30m свечах |
| `commission_pct` | `trading` | Комиссия брокера (0.0004 = 0.04%) |

---

## Структура проекта

```
traderbot/
├── main.py              # Точка входа
├── check.py             # Диагностика перед запуском
├── config.yaml          # Настройки
├── types.py             # Общие типы данных
├── broker/tbank.py      # Обёртка над T-Bank API
├── data/feed.py         # Загрузка свечей
├── strategies/          # Торговые стратегии (каждый файл = стратегия)
├── risk/manager.py      # Риск-менеджмент
├── execution/manager.py # Исполнение ордеров
├── notifications/       # Telegram уведомления
├── state/               # Хранение позиций (JSON)
├── journal/             # Журнал сделок (CSV)
└── backtest/            # Движок бэктеста
```

---

## Лучший результат бэктеста

**SBER, 30 дней:** 66.7% winrate, +17.96% к балансу (стратегия `ict`, 15-свечной lookback).
