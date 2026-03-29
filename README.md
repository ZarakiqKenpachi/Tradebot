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

### 2. Создать виртуальное окружение

```bash
python -3.12 -m venv venv
```

Активировать:
- **Windows:** `venv\Scripts\activate`
- **Linux/Mac:** `source venv/bin/activate`

### 3. Установить зависимости

> **Важно:** все команды запускать из корневой папки проекта, **не заходя** в `traderbot/`. Иначе Python перепутает наш файл `types.py` со стандартным модулем и выдаст ошибку импорта.

```bash
pip install -r traderbot/requirements.txt
```

> SDK от T-Bank (`t-tech-investments`) устанавливается автоматически с их PyPI-сервера.

### 4. Настроить переменные окружения

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

Бот автоматически выбирает нужный токен по флагу `sandbox` в `config.yaml`.

---

## Запуск

### Sandbox (тестовый режим, по умолчанию)

```bash
py -3.12 -m traderbot.main
```

> В `traderbot/config.yaml` по умолчанию `sandbox: true` — деньги реальные не тратятся.

### Live (реальная торговля)

1. Убедиться что `TBANK_LIVE_TOKEN` заполнен в `.env`
2. В `traderbot/config.yaml` поменять `sandbox: false`
3. Запустить:

```bash
py -3.12 -m traderbot.main
```

### Бэктест

```bash
py -3.12 -m traderbot.backtest.run --config traderbot/config.yaml --days 30
```

Результаты сохраняются в `traderbot/backtest/results/`.

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

Параметры `ict_v2_tester` редактируются прямо в `traderbot/strategies/ict_v2_tester.py` (константы наверху файла).

### Основные параметры конфига

| Параметр | Секция | Описание |
|---|---|---|
| `sandbox` | `broker` | `true` = тестовый режим, `false` = реальная торговля |
| `risk_pct` | `risk` | Риск на сделку (0.10 = 10% от баланса) |
| `max_position_pct` | `risk` | Макс. размер позиции (0.40 = 40%) |
| `max_consecutive_sl` | `risk` | Блокировка тикера после N стоп-лоссов подряд |
| `poll_interval_sec` | `trading` | Как часто проверять рынок (секунды) |
| `max_candles_timeout` | `trading` | Таймаут позиции в свечах |
| `commission_pct` | `trading` | Комиссия брокера (0.0004 = 0.04%) |

---

## Структура проекта

```
traderbot/
├── main.py              # Точка входа
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
