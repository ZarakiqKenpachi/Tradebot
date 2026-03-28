# TraderBot

Автономный торговый бот для MOEX (Московская биржа) через T-Bank Invest API.

**Стратегия:** ICT System — анализ структуры рынка на 1H таймфрейме.
**Инструменты по умолчанию:** SBER, GAZP, GMKN, VTBR, ROSN, NVTK, TATN, LKOH.

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

> **Важно:** все команды запускать из корневой папки проекта (`Tradebot-development/`), **не заходя** в `traderbot/`. Иначе Python перепутает наш файл `types.py` со стандартным модулем и выдаст ошибку импорта.

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
TBANK_TOKEN=your_tbank_token_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_telegram_chat_id_here
```

**Где взять токены:**
- `TBANK_TOKEN` — личный кабинет T-Bank Invest → Настройки → API → Sandbox токен
- `TELEGRAM_BOT_TOKEN` — создать бота через [@BotFather](https://t.me/BotFather) в Telegram
- `TELEGRAM_CHAT_ID` — написать что угодно своему боту, затем узнать ID через [@userinfobot](https://t.me/userinfobot)

---

## Запуск

### Живая торговля (sandbox по умолчанию)

```bash
py -3.12 traderbot/main.py
```

> В `traderbot/config.yaml` по умолчанию `sandbox: true` — деньги реальные не тратятся.
> Чтобы торговать реально, поменяй на `sandbox: false` и используй боевой токен.

### Бэктест

```bash
py -3.12 traderbot/backtest/run.py --config traderbot/config.yaml --days 30
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

Доступные стратегии: `ict`, `ict_v2_sw4_rr2`, `ict_v2_sw4_rr35`, `ict_v2_sw10_rr2`.

### Основные параметры

| Параметр | Где | Описание |
|---|---|---|
| `sandbox` | `broker` | `true` = тестовый режим без реальных денег |
| `risk_pct` | `risk` | Риск на сделку (0.10 = 10% от баланса) |
| `max_position_pct` | `risk` | Макс. размер позиции (0.40 = 40%) |
| `poll_interval_sec` | `trading` | Как часто проверять рынок (секунды) |
| `max_candles_timeout` | `trading` | Таймаут позиции в свечах |

---

## Структура проекта

```
traderbot/
├── main.py              # Точка входа
├── config.yaml          # Настройки
├── types.py             # Общие типы данных
├── broker/tbank.py      # Обёртка над T-Bank API
├── data/feed.py         # Загрузка свечей
├── strategies/          # Торговые стратегии
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
