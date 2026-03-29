from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass
class Setup:
    """Торговый сетап, возвращаемый стратегией."""
    direction: Signal
    entry_price: float
    stop_price: float
    target_price: float
    entry_reason: str


@dataclass
class Position:
    """Открытая позиция, хранимая в state и execution."""
    ticker: str
    figi: str
    direction: Signal
    entry_price: float
    stop_price: float
    target_price: float
    qty: int
    entry_time: datetime
    entry_reason: str
    entry_order_id: str
    sl_order_id: str = ""
    tp_order_id: str = ""
    status: str = "pending"  # "pending" → "active"
    candles_held: int = 0
    pending_candles: int = 0
    last_candle_time: datetime | None = None


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
    pnl: float
    commission: float
    entry_time: datetime
    exit_time: datetime
    entry_reason: str
    exit_reason: str
    candles_held: int
