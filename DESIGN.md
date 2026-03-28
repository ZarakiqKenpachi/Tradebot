# TraderBot v2 — Проектный документ

Полное описание архитектуры, логики и интерфейсов модулей.
Этот файл — единственный источник информации для реализации проекта с нуля.

---

## 1. Цель проекта

Автономный торговый бот для MOEX (Московская биржа) через T-Bank Invest API.
После запуска бот самостоятельно торгует по заданным стратегиям без вмешательства пользователя.

**Ключевые свойства:**
- Один тикер — одна стратегия (одна стратегия может обслуживать несколько тикеров)
- Конфиг-файл определяет какой тикер какой стратегией торгуется
- При сбое: автоматический перезапуск, восстановление открытых позиций
- Уведомления в Telegram (в будущем — управление через Telegram)
- Бэктест использует ту же логику что и live trading
- Журнал сделок в CSV с полными данными

---

## 2. Структура проекта

```
traderbot/
├── main.py                  # Точка входа live trading
├── config.yaml              # Конфигурация: тикеры, стратегии, параметры
├── config.py                # Загрузка и валидация конфига
├── types.py                 # Общие типы данных (Setup, Signal, Position)
│
├── broker/
│   └── tbank.py             # Обёртка над T-Bank Invest API
│
├── data/
│   └── feed.py              # Загрузка свечей + ресемплинг по таймфреймам
│
├── strategies/
│   ├── base.py              # Абстрактный класс BaseStrategy
│   ├── registry.py          # Реестр стратегий (имя → класс)
│   └── ict.py               # ICT стратегия
│
├── risk/
│   └── manager.py           # Расчёт размера позиции, лимиты
│
├── execution/
│   └── manager.py           # Размещение ордеров, трекинг позиций, восстановление
│
├── journal/
│   └── writer.py            # Запись сделок в CSV
│
├── notifications/
│   └── telegram.py          # Отправка уведомлений в Telegram
│
├── state/
│   └── store.py             # Персистентное хранение открытых позиций (JSON)
│
├── backtest/
│   ├── run.py               # Точка входа бэктеста (CLI)
│   ├── engine.py            # Движок симуляции (использует strategies/, risk/)
│   └── report.py            # Генерация отчётов и статистики
│
├── logs/                    # Директория для лог-файлов (runtime)
│   └── bot.log
│
├── requirements.txt
├── .env                     # Токены (не в git)
└── .env.example             # Шаблон .env
```

**Принцип:** каждая папка = один модуль с одной ответственностью.
Можно работать над `strategies/ict.py` не трогая ничего другого.

---

## 3. Конфигурация (config.yaml)

Формат YAML — читаемый, поддерживает комментарии, удобен для вложенных структур.

```yaml
# === Брокер ===
broker:
  token_env: "TBANK_TOKEN"        # Имя переменной окружения с токеном
  sandbox: true                    # true = песочница, false = реальная торговля
  app_name: "TraderBot"           # Идентификатор приложения для API

# === Риск-менеджмент ===
risk:
  risk_pct: 0.10                  # Макс. риск на сделку (% от баланса)
  max_position_pct: 0.40          # Макс. размер позиции (% от баланса)
  max_consecutive_sl: 3           # Макс. SL подряд на тикер → стоп торговли до след. дня

# === Торговые инструменты ===
tickers:
  SBER:
    figi: "BBG004730N88"
    strategy: "ict"               # Имя стратегии из реестра
  GAZP:
    figi: "BBG004730RP0"
    strategy: "ict"
  GMKN:
    figi: "BBG004731489"
    strategy: "ict"
  VTBR:
    figi: "BBG004730ZJ9"
    strategy: "ict"
  ROSN:
    figi: "BBG004731354"
    strategy: "ict"
  NVTK:
    figi: "BBG00475KKY8"
    strategy: "ict"

# === Уведомления ===
notifications:
  telegram:
    enabled: true
    token_env: "TELEGRAM_BOT_TOKEN"  # Имя переменной окружения
    chat_id_env: "TELEGRAM_CHAT_ID"  # Имя переменной окружения

# === Журнал сделок ===
journal:
  path: "journal/trades.csv"

# === Хранение состояния ===
state:
  path: "state/positions.json"

# === Основной цикл ===
trading:
  poll_interval_sec: 60           # Интервал опроса в секундах
  max_candles_timeout: 20         # Таймаут позиции (в 30m свечах)
  commission_pct: 0.00004         # Комиссия за сторону (0.004%)

# === Бэктест ===
backtest:
  initial_balance: 100000.0       # Стартовый баланс (RUB)
  days: 30                        # Глубина исторических данных
  output_dir: "backtest/results"  # Папка для CSV-результатов
```

---

## 4. Общие типы данных (types.py)

Единое место для всех структур данных, используемых между модулями.

```python
from dataclasses import dataclass
from enum import Enum
from datetime import datetime


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Setup:
    """Торговый сетап, возвращаемый стратегией."""
    direction: Signal             # Направление сделки
    entry_price: float            # Цена входа (лимитный ордер)
    stop_price: float             # Цена стоп-лосса
    target_price: float           # Цена тейк-профита
    entry_reason: str             # Описание причины входа (текст)


@dataclass
class Position:
    """Открытая позиция, хранимая в state и execution."""
    ticker: str                   # Тикер (SBER, GAZP...)
    figi: str                     # FIGI инструмента
    direction: Signal
    entry_price: float
    stop_price: float
    target_price: float
    qty: int                      # Количество лотов
    entry_time: datetime          # Время открытия
    entry_reason: str             # Причина входа
    entry_order_id: str           # ID лимитного ордера на вход
    sl_order_id: str              # ID стоп-ордера (SL)
    tp_order_id: str              # ID стоп-ордера (TP)
    candles_held: int = 0         # Счётчик свечей с момента входа


@dataclass
class TradeRecord:
    """Запись о завершённой сделке для журнала."""
    ticker: str
    figi: str
    direction: Signal
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    qty: int
    pnl: float                    # Прибыль/убыток (RUB)
    commission: float             # Комиссия (RUB)
    entry_time: datetime
    exit_time: datetime
    entry_reason: str             # Почему вошли
    exit_reason: str              # Почему вышли (take_profit / stop_loss / timeout)
    candles_held: int             # Сколько свечей держали позицию
```

---

## 5. Модуль: broker/tbank.py

Обёртка над T-Bank Invest API. Единственный модуль, который знает про API брокера.

### Зависимости
- Пакет: `tinkoff-investments` (актуальное название SDK для T-Bank)
- Переменные окружения: `TBANK_TOKEN`

### Класс: TBankBroker

```python
class TBankBroker:
    def __init__(self, token: str, sandbox: bool = True, app_name: str = "TraderBot")
```

### Методы

**Данные:**
```python
def get_candles(figi: str, interval: CandleInterval, days: int) -> pd.DataFrame
    """
    Получить свечи за последние N дней.
    Возвращает DataFrame с колонками: time (UTC, datetime), open, high, low, close, volume.
    Для бэктеста использует пагинацию (get_all_candles).
    Для live — одиночный запрос.
    interval: CANDLE_INTERVAL_1_MIN, _30_MIN, _HOUR, _4_HOUR
    """

def get_candles_history(figi: str, interval: CandleInterval, days: int) -> pd.DataFrame
    """
    Получить полную историю свечей с пагинацией (для бэктеста).
    Аналогичен get_candles, но использует get_all_candles() SDK
    для получения больших объёмов данных без потерь.
    """
```

**Аккаунт:**
```python
def get_account_id() -> str
    """Получить ID первого доступного аккаунта."""

def get_portfolio_balance(account_id: str) -> float
    """Получить баланс портфеля в RUB."""
```

**Ордера:**
```python
def place_limit_order(account_id, figi, qty, direction, price) -> str
    """Разместить лимитный ордер. Вернуть order_id."""

def place_stop_loss(account_id, figi, qty, direction, stop_price) -> str
    """Разместить стоп-лосс на бирже (GTC). Вернуть stop_order_id."""

def place_take_profit(account_id, figi, qty, direction, tp_price) -> str
    """Разместить тейк-профит на бирже (GTC). Вернуть stop_order_id."""

def cancel_order(account_id, order_id) -> None
    """Отменить обычный ордер."""

def cancel_stop_order(account_id, stop_order_id) -> None
    """Отменить стоп-ордер."""

def get_order_state(account_id, order_id) -> OrderState
    """Получить статус ордера (для проверки исполнения)."""

def get_stop_orders(account_id) -> list
    """Получить список активных стоп-ордеров (для recovery)."""
```

**Песочница:**
```python
def sandbox_create_account(name: str = "TraderBot") -> str
    """Создать аккаунт в песочнице. Вернуть account_id."""

def sandbox_fund(account_id: str, amount: float) -> None
    """Пополнить аккаунт в песочнице (RUB)."""
```

### Вспомогательные функции (приватные)
```python
def _quotation_to_float(q: Quotation) -> float
def _float_to_quotation(value: float) -> Quotation
def _candles_to_dataframe(candles: list) -> pd.DataFrame
```

---

## 6. Модуль: data/feed.py

Загрузка данных и ресемплинг. Готовит данные в формате, нужном стратегиям.

### Класс: DataFeed

```python
class DataFeed:
    def __init__(self, broker: TBankBroker)
```

### Методы

```python
def get_candles(figi: str, timeframes: list[str], days: int) -> dict[str, pd.DataFrame]
    """
    Получить свечи для указанных таймфреймов.

    Логика:
    1. Загружает 1m свечи из broker.get_candles(figi, 1m, days)
    2. Ресемплит в каждый из запрошенных таймфреймов
    3. Фильтрует по часам работы MOEX
    4. Возвращает словарь: {"30m": df_30m, "1h": df_1h, "4h": df_4h}

    timeframes: список из ["1m", "30m", "1h", "4h"]
    """

def get_candles_history(figi: str, timeframes: list[str], days: int) -> dict[str, pd.DataFrame]
    """
    Аналогично get_candles, но с пагинацией для бэктеста.
    Использует broker.get_candles_history().
    """
```

### Вспомогательные функции

```python
def resample(df_1m: pd.DataFrame, freq: str) -> pd.DataFrame
    """
    Ресемплинг 1m свечей в заданный таймфрейм.
    freq: "30min", "1h", "4h"
    Агрегация: open=first, high=max, low=min, close=last, volume=sum
    Удаляет строки с NaN.
    """

def filter_moex_hours(df: pd.DataFrame) -> pd.DataFrame
    """
    Оставить только свечи в рабочие часы MOEX.
    Рабочие часы: Пн-Пт, 07:00-23:59 МСК (UTC+3).
    1. Конвертировать time из UTC в MSK
    2. Отфильтровать выходные (сб, вс)
    3. Отфильтровать часы вне 07:00-23:59
    """
```

### Формат DataFrame (единый для всех таймфреймов)

| Колонка  | Тип       | Описание                       |
|----------|-----------|--------------------------------|
| time     | datetime  | Время открытия свечи (UTC)     |
| open     | float     | Цена открытия                  |
| high     | float     | Максимум                       |
| low      | float     | Минимум                        |
| close    | float     | Цена закрытия                  |
| volume   | int       | Объём                          |

DataFrame индексирован по колонке `time`.

---

## 7. Модуль: strategies/

### 7.1 base.py — Абстрактный класс

```python
from abc import ABC, abstractmethod
import pandas as pd
from types import Setup


class BaseStrategy(ABC):
    """Базовый класс для всех торговых стратегий."""

    # Каждая стратегия объявляет нужные ей таймфреймы.
    # DataFeed будет готовить именно эти таймфреймы.
    required_timeframes: list[str]  # Пример: ["30m", "1h"]

    @abstractmethod
    def find_setup(self, candles: dict[str, pd.DataFrame]) -> Setup | None:
        """
        Анализировать данные и найти торговый сетап.

        candles: словарь {таймфрейм: DataFrame}
                 Пример: {"30m": df_30m, "1h": df_1h, "4h": df_4h}

        Возвращает:
            Setup — если найден валидный сетап
            None — если сетапа нет
        """
        pass
```

### 7.2 registry.py — Реестр стратегий

Маппинг имени стратегии из конфига → класс стратегии.

```python
from strategies.base import BaseStrategy
from strategies.ict import ICTStrategy

# Реестр: имя из config.yaml → класс стратегии
STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "ict": ICTStrategy,
}


def get_strategy(name: str) -> BaseStrategy:
    """
    Создать экземпляр стратегии по имени.
    Бросает KeyError если стратегия не найдена.
    """
    cls = STRATEGY_REGISTRY[name]
    return cls()
```

**Добавление новой стратегии:**
1. Создать файл `strategies/my_strategy.py`
2. Реализовать класс, наследуясь от `BaseStrategy`
3. Добавить строку в `STRATEGY_REGISTRY` в `registry.py`
4. Указать имя в `config.yaml` для нужных тикеров

### 7.3 ict.py — ICT стратегия (System Variant A)

Адаптированная модель ликвидности на основе ICT-концепций.
Не классический ICT — нет HTF bias, нет «идеальной структуры».
Суть: локальный sweep → импульс → возврат → вход → забор RR 2.

**Основные параметры системы:**
- TF анализа: 1H
- TF входа: 30m
- Risk: 10% на сделку
- RR: 1:2
- Hold: макс 20 свечей 30m
- Bias: НЕ используется
- Второй ретест: разрешён

```python
class ICTStrategy(BaseStrategy):
    required_timeframes = ["30m", "1h"]
```

#### Параметры (захардкожены в файле)

```python
SWEEP_LOOKBACK = 15                     # Кол-во 1H свечей для определения структуры
DISPLACEMENT_MIN_BODY_RATIO = 0.35      # Минимальное соотношение тело/диапазон свечи
DISPLACEMENT_MIN_ATR_RATIO = 0.70       # Мин. диапазон displacement в долях ATR(14)
ENTRY_RETRACEMENT = 0.50                # Вход на 50% ретрейсмента тела импульсной свечи
STOP_BUFFER = 0.003                     # 0.3% буфер за уровнем свипа
MIN_SL_DISTANCE = 0.003                 # Минимальное расстояние до SL (0.3% от цены)
RISK_REWARD = 2.0                       # Соотношение риск/прибыль (1:2)
```

#### Логика: find_setup(candles) → Setup | None

**Шаг 1: Обнаружение свипа на 1H**

```
df_1h = candles["1h"]
Требование: len(df_1h) >= SWEEP_LOOKBACK + 1 (16 свечей)

sweep_candle = df_1h.iloc[-1]          # Последняя 1H свеча
structure = df_1h.iloc[-16:-1]         # Предыдущие 15 свечей

Бычий свип (BUY):
  ЕСЛИ sweep_candle.low < structure.low.min()
       И sweep_candle.close > structure.low.min()
  ТО: direction = BUY, sweep_level = structure.low.min()

Медвежий свип (SELL):
  ЕСЛИ sweep_candle.high > structure.high.max()
       И sweep_candle.close < structure.high.max()
  ТО: direction = SELL, sweep_level = structure.high.max()

Если свипа нет → return None
```

**Шаг 2: Поиск импульсной свечи (displacement) на 30m**

```
df_30m = candles["30m"]
sweep_time = sweep_candle.time
after_sweep = df_30m[df_30m.index >= sweep_time]
Требование: len(after_sweep) >= 1

# Рассчитать ATR(14) на 30m
tr = max(high - low, abs(high - prev_close), abs(low - prev_close))  # True Range
atr_14 = tr.rolling(14).mean()  # Средний True Range за 14 периодов

Для каждой 30m свечи после свипа:
  candle_range = high - low
  body = abs(close - open)

  # Фильтр: тело достаточно большое относительно диапазона
  ЕСЛИ body / candle_range < DISPLACEMENT_MIN_BODY_RATIO (0.35): ПРОПУСТИТЬ

  # Фильтр: диапазон достаточно большой относительно ATR (не шум/флэт)
  ЕСЛИ candle_range < atr_14 * DISPLACEMENT_MIN_ATR_RATIO (0.70): ПРОПУСТИТЬ

  Для BUY (бычий displacement):
    ЕСЛИ close > open:
      entry_price = close - 0.50 * body
      stop_price = sweep_level * (1 - 0.003)
      risk = entry_price - stop_price

      ЕСЛИ risk > 0 И risk / entry_price >= 0.003:
        target_price = entry_price + RISK_REWARD * risk
        ВЕРНУТЬ Setup(BUY, entry_price, stop_price, target_price, reason)

  Для SELL (медвежий displacement):
    ЕСЛИ close < open:
      entry_price = close + 0.50 * body
      stop_price = sweep_level * (1 + 0.003)
      risk = stop_price - entry_price

      ЕСЛИ risk > 0 И risk / entry_price >= 0.003:
        target_price = entry_price - RISK_REWARD * risk
        ВЕРНУТЬ Setup(SELL, entry_price, stop_price, target_price, reason)

Если displacement не найден → return None
```

**Шаг 3: Формирование entry_reason**

Текстовое описание на русском языке:
```
"1H бычий свип ниже {sweep_level:.2f} (структурный минимум {SWEEP_LOOKBACK} свечей);
30m импульсная свеча вверх (тело {body_pct:.0f}% диапазона, диапазон {atr_pct:.0f}% ATR);
вход на {ENTRY_RETRACEMENT*100:.0f}% ретрейсменте импульса"
```

#### Фильтры (обязательные)

**Минимальная дистанция SL:**
Если расстояние до SL < 0.3% от цены → сетап пропускается (шум).
Реализовано в шаге 2: `risk / entry_price >= MIN_SL_DISTANCE`.

**ATR-фильтр (флэт):**
Displacement свеча должна иметь диапазон ≥ 0.7 × ATR(14) на 30m.
В условиях флэта displacement не пройдёт этот фильтр → торговля не ведётся.
Реализовано в шаге 2: `candle_range >= atr_14 * DISPLACEMENT_MIN_ATR_RATIO`.

**Второй sweep против позиции:**
Если после входа цена снова делает sweep в направлении, противоположном позиции —
сделка НЕ отменяется автоматически. Фильтр разрешающий, не блокирующий.

#### Повторный вход (второй ретест)

Разрешён второй ретест зоны входа, если:
- Лимитка первого входа не исполнилась
- Цена НЕ обновила экстремум (не пробила sweep_level повторно)

Если цена обновляет экстремум → сетап аннулируется:
- Для SHORT: новый high выше sweep_level
- Для LONG: новый low ниже sweep_level

Реализация: стратегия запоминает последний сетап (`pending_setup`).
При повторном вызове `find_setup()` проверяет — если `pending_setup` активен
и экстремум не обновлён → возвращает тот же Setup повторно.

#### Известные риски системы

- Переоптимизация параметров на исторических данных
- Второй ретест может не работать на других рынках/инструментах

---

## 8. Модуль: risk/manager.py

### Класс: RiskManager

```python
class RiskManager:
    def __init__(self, risk_pct: float = 0.10, max_position_pct: float = 0.40)
```

### Метод: position_size

```python
def position_size(self, balance: float, entry_price: float, stop_price: float) -> int:
    """
    Рассчитать размер позиции в лотах.

    Логика:
    1. risk_amount = balance * risk_pct
    2. risk_per_unit = abs(entry_price - stop_price)
    3. qty = floor(risk_amount / risk_per_unit)
    4. max_qty = floor(balance * max_position_pct / entry_price)
    5. return min(qty, max_qty), но не менее 1

    Если risk_per_unit == 0: return 0
    Если вычисленный qty < 1: return 0
    """
```

---

## 9. Модуль: execution/manager.py

Управление жизненным циклом позиций. Связывает стратегию, брокер, риск и журнал.

### Класс: ExecutionManager

```python
class ExecutionManager:
    def __init__(
        self,
        broker: TBankBroker,
        risk: RiskManager,
        journal: TradeJournal,
        state: StateStore,
        notifier: TelegramNotifier | None,
        account_id: str,
        commission_pct: float,
        max_candles_timeout: int,
        max_consecutive_sl: int = 3,
    )
```

### Состояние

Позиции хранятся в `StateStore` (JSON на диске) для восстановления после перезапуска.
В памяти — `dict[str, Position]` (figi → Position).

### Методы

**Проверка позиции:**
```python
def has_position(self, figi: str) -> bool
    """Есть ли открытая позиция по данному инструменту."""

def is_ticker_blocked(self, ticker: str) -> bool
    """
    Проверить, заблокирован ли тикер из-за серии SL.

    Возвращает True если:
    - Количество подряд SL >= max_consecutive_sl
    - И последний SL был сегодня (блокировка до следующего торгового дня)

    Логика:
    1. count, last_date = state.get_consecutive_sl(ticker)
    2. Если last_date == сегодня И count >= max_consecutive_sl → True
    3. Иначе → False
    """
```

**Открытие позиции:**
```python
def open_position(self, ticker: str, figi: str, setup: Setup) -> None:
    """
    Открыть новую позицию.

    Шаги:
    1. Запросить баланс: broker.get_portfolio_balance(account_id)
    2. Рассчитать размер: risk.position_size(balance, entry_price, stop_price)
    3. Если qty < 1: отменить (залогировать предупреждение)
    4. Разместить лимитный ордер на вход: broker.place_limit_order(...)
    5. Разместить SL на бирже (GTC): broker.place_stop_loss(...)
    6. Разместить TP на бирже (GTC): broker.place_take_profit(...)
    7. Создать Position, сохранить в state
    8. Отправить уведомление в Telegram:
       "🟢 Открыта позиция {ticker} {direction}
        Вход: {entry_price} | SL: {stop_price} | TP: {target_price}
        Объём: {qty} | Причина: {entry_reason}"
    """
```

**Обновление позиции (вызывается каждый цикл):**
```python
def update(self, figi: str, current_price: float) -> None:
    """
    Обновить состояние открытой позиции.

    Шаги:
    1. Увеличить candles_held
    2. Проверить таймаут: candles_held >= max_candles_timeout
    3. Если таймаут → _close_position(figi, current_price, "timeout")
    4. Обновить state на диске

    Примечание: SL и TP исполняются на бирже автоматически.
    Бот отслеживает только таймаут. При следующем цикле
    recovery-логика обнаружит что SL/TP ордера исчезли
    и зафиксирует закрытие.
    """
```

**Закрытие позиции:**
```python
def _close_position(self, figi: str, exit_price: float, reason: str) -> None:
    """
    Закрыть позицию.

    Шаги:
    1. Отменить оставшиеся ордера на бирже (SL, TP, entry если не исполнен)
    2. Если таймаут — разместить рыночный ордер на закрытие
    3. Рассчитать PnL:
       BUY:  pnl = (exit_price - entry_price) * qty
       SELL: pnl = (entry_price - exit_price) * qty
    4. Рассчитать комиссию:
       commission = 2 * commission_pct * avg_price * qty
       pnl_net = pnl - commission
    5. Записать в журнал: journal.log_trade(TradeRecord(...))
    6. Удалить из state
    7. Обновить счётчик подряд SL:
       - Если reason == "stop_loss": state.increment_consecutive_sl(ticker)
       - Если reason == "take_profit" или "timeout": state.reset_consecutive_sl(ticker)
    8. Отправить уведомление в Telegram:
       "🔴 Закрыта позиция {ticker} {direction}
        Вход: {entry_price} → Выход: {exit_price}
        P&L: {pnl_net:+.2f} RUB | Причина: {reason}
        Длительность: {candles_held} свечей"
    """
```

**Восстановление после перезапуска:**
```python
def recover(self) -> None:
    """
    Вызывается при старте бота. Восстанавливает позиции из state.

    Шаги:
    1. Загрузить позиции из state (JSON)
    2. Для каждой позиции:
       a. Проверить активные стоп-ордера на бирже: broker.get_stop_orders()
       b. Если SL и TP ордера существуют:
          → Позиция активна, продолжить трекинг
       c. Если SL или TP ордера отсутствуют:
          → Позиция была закрыта пока бот не работал
          → Определить цену закрытия (последняя доступная)
          → Определить причину (сработал SL или TP)
          → _close_position() для фиксации в журнале
    3. Сбросить устаревшие счётчики SL: state.reset_stale_sl_counters()
       (счётчики с last_sl_date < сегодня обнуляются — новый торговый день)
    4. Залогировать результат recovery
    5. Отправить уведомление: "Бот перезапущен. Активных позиций: N"
    """
```

---

## 10. Модуль: journal/writer.py

### Класс: TradeJournal

```python
class TradeJournal:
    def __init__(self, path: str = "journal/trades.csv")
```

### Формат CSV

```
ticker,figi,direction,entry_price,exit_price,stop_price,target_price,qty,pnl,commission,entry_time,exit_time,entry_reason,exit_reason,candles_held
SBER,BBG004730N88,BUY,315.00,318.50,312.00,321.00,100,350.00,2.52,2026-03-27T10:30:00+03:00,2026-03-27T17:00:00+03:00,"1H бычий свип...",take_profit,13
```

### Методы

```python
def log_trade(self, record: TradeRecord) -> None:
    """
    Записать сделку в CSV.

    Шаги:
    1. Если файл не существует — создать с заголовком
    2. Добавить строку в конец файла (append mode)
    3. Вывести в консоль: [JOURNAL] {direction} {ticker} | pnl={pnl}
    """

def read_trades(self) -> pd.DataFrame:
    """
    Прочитать все сделки из CSV в DataFrame.
    Используется для отчётов в бэктесте.
    """
```

---

## 11. Модуль: notifications/telegram.py

### Класс: TelegramNotifier

```python
class TelegramNotifier:
    def __init__(self, token: str, chat_id: str)
```

### Методы

```python
def send(self, message: str) -> None:
    """
    Отправить текстовое сообщение в Telegram.
    Использует HTTP API: POST https://api.telegram.org/bot{token}/sendMessage
    При ошибке — логировать, но НЕ прерывать работу бота.
    """
```

### Когда отправляются уведомления

| Событие                      | Сообщение                                           |
|------------------------------|-----------------------------------------------------|
| Бот запущен                  | "Бот запущен. Режим: {sandbox/live}. Тикеров: N"  |
| Бот перезапущен              | "Бот перезапущен. Активных позиций: N"             |
| Позиция открыта              | Тикер, направление, цены, объём, причина            |
| Позиция закрыта              | Тикер, цены входа/выхода, P&L, причина закрытия    |
| Ошибка                       | Описание ошибки, стектрейс (кратко)                |
| Критическая ошибка + рестарт | "Критическая ошибка: {error}. Перезапуск..."       |

---

## 12. Модуль: state/store.py

Персистентное хранение состояния для восстановления после перезапуска.

### Класс: StateStore

```python
class StateStore:
    def __init__(self, path: str = "state/positions.json")
```

### Формат JSON

```json
{
  "positions": {
    "BBG004730N88": {
      "ticker": "SBER",
      "figi": "BBG004730N88",
      "direction": "BUY",
      "entry_price": 315.0,
      "stop_price": 312.0,
      "target_price": 321.0,
      "qty": 100,
      "entry_time": "2026-03-27T10:30:00+03:00",
      "entry_reason": "1H бычий свип...",
      "entry_order_id": "uuid-1234",
      "sl_order_id": "uuid-5678",
      "tp_order_id": "uuid-9012",
      "candles_held": 5
    }
  },
  "consecutive_sl": {
    "SBER": {"count": 2, "last_sl_date": "2026-03-27"},
    "GAZP": {"count": 0, "last_sl_date": null}
  },
  "last_updated": "2026-03-27T15:00:00+03:00"
}
```

### Методы

```python
def save_position(self, position: Position) -> None:
    """Сохранить/обновить позицию в файле."""

def remove_position(self, figi: str) -> None:
    """Удалить позицию из файла."""

def get_positions(self) -> dict[str, Position]:
    """Загрузить все позиции из файла."""

def update_candles_held(self, figi: str, candles_held: int) -> None:
    """Обновить счётчик свечей для позиции."""

def increment_consecutive_sl(self, ticker: str) -> None:
    """Увеличить счётчик подряд SL для тикера. Записать текущую дату."""

def reset_consecutive_sl(self, ticker: str) -> None:
    """Сбросить счётчик подряд SL для тикера (после TP или timeout)."""

def get_consecutive_sl(self, ticker: str) -> tuple[int, str | None]:
    """Получить (count, last_sl_date) для тикера."""

def reset_stale_sl_counters(self) -> None:
    """Сбросить счётчики SL, у которых last_sl_date < сегодня (новый торговый день)."""
```

**Важно:** каждая запись/удаление сразу сбрасывается на диск (flush).
Это гарантирует, что при аварийном завершении данные не потеряются.

---

## 13. Модуль: config.py

```python
import yaml
import os
from dataclasses import dataclass


@dataclass
class BrokerConfig:
    token: str                    # Значение токена (прочитано из env)
    sandbox: bool
    app_name: str

@dataclass
class TickerConfig:
    figi: str
    strategy: str                 # Имя стратегии из реестра

@dataclass
class TelegramConfig:
    enabled: bool
    token: str                    # Значение токена (прочитано из env)
    chat_id: str                  # Значение chat_id (прочитано из env)

@dataclass
class AppConfig:
    broker: BrokerConfig
    risk_pct: float
    max_position_pct: float
    max_consecutive_sl: int            # Макс. SL подряд на тикер
    tickers: dict[str, TickerConfig]   # ticker_name → TickerConfig
    telegram: TelegramConfig
    journal_path: str
    state_path: str
    poll_interval_sec: int
    max_candles_timeout: int
    commission_pct: float
    backtest_initial_balance: float
    backtest_days: int
    backtest_output_dir: str


def load_config(path: str = "config.yaml") -> AppConfig:
    """
    Загрузить конфиг из YAML и подставить значения из .env.

    Шаги:
    1. Прочитать YAML файл
    2. Для полей *_env — получить значение из os.environ
    3. Валидировать обязательные поля
    4. Вернуть AppConfig
    """
```

---

## 14. Точка входа: main.py (Live Trading)

```python
"""
Точка входа для live-торговли.
Запуск: python main.py
"""

def main():
    # 1. Загрузить конфиг
    config = load_config("config.yaml")

    # 2. Инициализировать модули
    broker = TBankBroker(config.broker.token, config.broker.sandbox, config.broker.app_name)
    feed = DataFeed(broker)
    risk = RiskManager(config.risk_pct, config.max_position_pct)
    journal = TradeJournal(config.journal_path)
    state = StateStore(config.state_path)
    notifier = TelegramNotifier(config.telegram.token, config.telegram.chat_id)
                if config.telegram.enabled else None

    # 3. Подготовить аккаунт
    if config.broker.sandbox:
        account_id = broker.sandbox_create_account()
        broker.sandbox_fund(account_id, 100000.0)
    else:
        account_id = broker.get_account_id()

    # 4. Инициализировать стратегии для каждого тикера
    # strategies: dict[str, BaseStrategy]  — ticker_name → экземпляр стратегии
    strategies = {}
    for ticker_name, ticker_conf in config.tickers.items():
        strategies[ticker_name] = get_strategy(ticker_conf.strategy)

    # 5. Инициализировать execution manager
    execution = ExecutionManager(
        broker=broker,
        risk=risk,
        journal=journal,
        state=state,
        notifier=notifier,
        account_id=account_id,
        commission_pct=config.commission_pct,
        max_candles_timeout=config.max_candles_timeout,
        max_consecutive_sl=config.max_consecutive_sl,
    )

    # 6. Восстановить открытые позиции после перезапуска
    execution.recover()

    # 7. Уведомить о запуске
    if notifier:
        mode = "sandbox" if config.broker.sandbox else "live"
        notifier.send(f"Бот запущен. Режим: {mode}. Тикеров: {len(config.tickers)}")

    # 8. Основной цикл
    while True:
        try:
            for ticker_name, ticker_conf in config.tickers.items():
                figi = ticker_conf.figi
                strategy = strategies[ticker_name]

                # Получить данные для нужных таймфреймов
                candles = feed.get_candles(
                    figi=figi,
                    timeframes=strategy.required_timeframes,
                    days=3,  # 3 дня для live достаточно
                )

                if not candles:
                    continue

                # Текущая цена (close последней свечи минимального таймфрейма)
                min_tf = strategy.required_timeframes[0]
                current_price = candles[min_tf].iloc[-1]["close"]

                if execution.has_position(figi):
                    # Обновить открытую позицию (проверить таймаут)
                    execution.update(figi, current_price)
                elif execution.is_ticker_blocked(ticker_name):
                    # Тикер заблокирован (3+ SL подряд сегодня) — пропустить
                    continue
                else:
                    # Искать новый сетап
                    setup = strategy.find_setup(candles)
                    if setup:
                        execution.open_position(ticker_name, figi, setup)

            # Пауза между циклами
            time.sleep(config.poll_interval_sec)

        except KeyboardInterrupt:
            print("Бот остановлен пользователем.")
            break
        except Exception as e:
            # Логировать ошибку
            logging.exception(f"Ошибка в основном цикле: {e}")
            if notifier:
                notifier.send(f"Ошибка: {e}")
            # Продолжить работу после паузы
            time.sleep(config.poll_interval_sec)
```

### Автоматический перезапуск

Обеспечивается внешним механизмом. Варианты:
- **Простой:** bash-скрипт с `while true; do python main.py; sleep 5; done`
- **Продвинутый:** systemd unit (Linux) или Task Scheduler (Windows)
- **Python:** модуль `supervisor` или обёртка с `subprocess`

Внутри `main.py` — бот ловит исключения в основном цикле и продолжает работу.
Критические ошибки (потеря токена, невозможность подключения) — завершают процесс,
и внешний механизм перезапускает его.

---

## 15. Бэктест: backtest/

Весь код бэктеста изолирован в одной папке. Использует те же модули
strategies/, risk/ и data/ что и live trading.

### 15.1 backtest/run.py — Точка входа

```
Запуск: python -m backtest.run
        python -m backtest.run --tickers SBER,GAZP --days 60 --rr 2.0,3.5
```

```python
def main():
    # 1. Загрузить конфиг (для тикеров и стратегий)
    config = load_config("config.yaml")

    # 2. Парсить CLI-аргументы (опционально переопределяют конфиг)
    #    --tickers: список тикеров (по умолчанию все из конфига)
    #    --days: глубина данных (по умолчанию из конфига)
    #    --rr: значения risk/reward для тестирования

    # 3. Инициализировать брокер и DataFeed
    broker = TBankBroker(token, sandbox=True)
    feed = DataFeed(broker)

    # 4. Загрузить данные для всех тикеров
    all_data = {}
    for ticker_name, ticker_conf in selected_tickers.items():
        strategy = get_strategy(ticker_conf.strategy)
        all_data[ticker_name] = feed.get_candles_history(
            figi=ticker_conf.figi,
            timeframes=strategy.required_timeframes,
            days=days,
        )

    # 5. Запустить симуляцию
    engine = BacktestEngine(config)
    results = engine.run(all_data)

    # 6. Вывести отчёт
    report = BacktestReport(results)
    report.print_summary()
    report.export_csv(config.backtest_output_dir)
```

### 15.2 backtest/engine.py — Движок симуляции

```python
class BacktestEngine:
    def __init__(self, config: AppConfig)

    def run(self, all_data: dict) -> list[TradeRecord]:
        """
        Прогнать симуляцию по историческим данным.

        Логика (аналогична live trading, но без реальных ордеров):

        1. Определить общую временную шкалу (master timeline)
           по самому длинному набору 30m данных

        2. Для каждого шага времени:
           a. Для каждого тикера с открытой позицией:
              - candles_held += 1
              - Проверить SL: цена пробила stop_price
              - Проверить TP: цена достигла target_price
              - Проверить таймаут: candles_held >= max_candles_timeout
              - Если сработало → закрыть, посчитать PnL с комиссией

           b. Для каждого тикера без позиции:
              - Подготовить окно данных до текущего момента
              - strategy.find_setup(candles_window)
              - Если сетап найден:
                → risk.position_size(balance, entry, stop)
                → Если qty >= 1: открыть виртуальную позицию

        3. Вернуть список всех TradeRecord
        """
```

### 15.3 backtest/report.py — Отчёты

```python
class BacktestReport:
    def __init__(self, trades: list[TradeRecord], initial_balance: float)

    def print_summary(self) -> None:
        """
        Вывести в консоль:
        - Общая статистика: баланс, P&L, доходность%, кол-во сделок, винрейт
        - По тикерам: сделки, W/L, винрейт%, P&L
        - Топ-5 лучших и худших сделок
        - Разбивка по причинам закрытия (TP / SL / timeout)
        """

    def export_csv(self, output_dir: str) -> None:
        """
        Экспортировать {TICKER}_trades.csv для каждого тикера.
        Колонки: entry_time, exit_time, direction, entry, exit, stop,
                 target, qty, pnl, commission, balance_after,
                 exit_reason, candles_held, entry_reason
        """
```

---

## 16. Фильтрация по времени MOEX

Константы (используются в data/feed.py):

```python
MOEX_OPEN_HOUR_MSK = 7     # 07:00 по Москве
MOEX_CLOSE_HOUR_MSK = 24   # до 23:59 по Москве (включительно 23-й час)
MOEX_TIMEZONE = "Europe/Moscow"  # UTC+3
```

Логика фильтрации:
1. Конвертировать UTC → MSK
2. Оставить только Пн-Пт (weekday 0-4)
3. Оставить только часы 07:00-23:59

---

## 17. Поток данных (Live Trading)

```
main.py
  │
  ├─ config.yaml ──→ config.py ──→ AppConfig
  │
  ├─ Инициализация модулей
  │     broker/tbank.py    ← T-Bank API
  │     data/feed.py       ← Использует broker
  │     strategies/ict.py  ← Из реестра по конфигу
  │     risk/manager.py    ← Параметры из конфига
  │     execution/manager.py ← Связывает всё
  │     journal/writer.py  ← CSV файл
  │     state/store.py     ← JSON файл
  │     notifications/telegram.py ← HTTP API
  │
  ├─ execution.recover()  ← Восстановить позиции из state
  │
  └─ Основной цикл (каждые 60 сек):
       │
       └─ Для каждого тикера:
            │
            ├─ feed.get_candles(figi, timeframes, days=3)
            │     └─ broker.get_candles(figi, 1m, 3)
            │     └─ resample → {30m, 1h, 4h}
            │     └─ filter_moex_hours()
            │
            ├─ Если есть позиция:
            │     └─ execution.update(figi, price)
            │           ├─ candles_held += 1
            │           ├─ Таймаут? → _close_position()
            │           │                ├─ Отмена ордеров
            │           │                ├─ PnL + комиссия
            │           │                ├─ journal.log_trade()
            │           │                ├─ state.remove_position()
            │           │                └─ notifier.send()
            │           └─ Обновить state
            │
            └─ Если нет позиции:
                  └─ strategy.find_setup(candles)
                        └─ Если Setup найден:
                              └─ execution.open_position()
                                    ├─ broker.get_portfolio_balance()
                                    ├─ risk.position_size()
                                    ├─ broker.place_limit_order()
                                    ├─ broker.place_stop_loss()
                                    ├─ broker.place_take_profit()
                                    ├─ state.save_position()
                                    └─ notifier.send()
```

---

## 18. Поток данных (Backtest)

```
backtest/run.py
  │
  ├─ config.yaml + CLI args
  │
  ├─ Загрузка исторических данных:
  │     feed.get_candles_history(figi, timeframes, days)
  │
  └─ backtest/engine.py ── Симуляция:
       │
       ├─ Использует: strategy.find_setup() ← тот же код что и live
       ├─ Использует: risk.position_size()  ← тот же код что и live
       ├─ НЕ использует: broker (ордера симулируются)
       ├─ НЕ использует: state (нет персистентности)
       ├─ НЕ использует: notifications
       │
       └─ backtest/report.py ── Вывод:
            ├─ Консольные таблицы
            └─ CSV файлы в backtest/results/
```

---

## 19. Обработка ошибок

| Ситуация | Действие |
|----------|----------|
| Ошибка API при получении данных | Лог + пропустить тикер в этом цикле |
| Ошибка API при размещении ордера | Лог + уведомление + пропустить |
| Потеря соединения | Лог + уведомление + retry через poll_interval |
| Невалидный конфиг | Завершить работу с ошибкой при старте |
| Ошибка Telegram | Лог в файл, НЕ прерывать работу |
| Неожиданное исключение в цикле | Лог + уведомление + продолжить цикл |
| Критическая ошибка (токен невалиден) | Завершить процесс → внешний перезапуск |

### Логирование

Используется стандартный `logging` Python:
- Файл: `logs/bot.log`
- Ротация: по размеру (10 MB, 5 файлов)
- Уровни: DEBUG для отладки, INFO для событий, ERROR для ошибок
- Формат: `[2026-03-27 10:30:00] [INFO] [execution] Открыта позиция SBER BUY @ 315.0`

---

## 20. Зависимости (requirements.txt)

```
tinkoff-investments       # T-Bank Invest gRPC SDK
pandas                    # DataFrames, ресемплинг
python-dotenv             # Загрузка .env
pyyaml                    # Парсинг config.yaml
requests                  # HTTP для Telegram API
```

---

## 21. .env.example

```env
TBANK_TOKEN=your_tbank_invest_token_here
TELEGRAM_BOT_TOKEN=your_telegram_bot_token_here
TELEGRAM_CHAT_ID=your_chat_id_here
```

---

## 22. Чеклист добавления новой стратегии

1. Создать файл `strategies/my_strategy.py`
2. Определить класс, наследуясь от `BaseStrategy`:
   ```python
   class MyStrategy(BaseStrategy):
       required_timeframes = ["30m", "4h"]

       def find_setup(self, candles):
           # Логика стратегии
           ...
   ```
3. Добавить в `strategies/registry.py`:
   ```python
   from strategies.my_strategy import MyStrategy
   STRATEGY_REGISTRY["my_strategy"] = MyStrategy
   ```
4. В `config.yaml` назначить стратегию тикерам:
   ```yaml
   tickers:
     SBER:
       figi: "BBG004730N88"
       strategy: "my_strategy"
   ```

---

## 23. Контекст из опыта v1

### Что работало хорошо
- SL/TP на бирже (GTC) — защита даже при падении бота
- Ресемплинг из 1m в старшие таймфреймы — один запрос к API
- Фильтрация по часам MOEX — избавляет от аномальных свечей
- Отдельные CSV на тикер — удобно для анализа
- entry_reason на русском — понятно при просмотре журнала

### Что нужно улучшить (уже учтено в v2)
- Монолитный backtest.py → разделён на engine + report
- Стратегия захардкожена для всех тикеров → конфиг с маппингом
- Нет восстановления позиций → state store + recovery
- Нет уведомлений → Telegram
- Параметры разбросаны по файлам → стратегия хранит свои, общие в конфиге
- Таймфреймы фиксированы → стратегия объявляет required_timeframes
