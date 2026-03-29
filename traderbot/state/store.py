import json
import logging
import os
from datetime import datetime, date

from traderbot.types import Position, Signal

logger = logging.getLogger(__name__)


class StateStore:
    """Персистентное хранение состояния для восстановления после перезапуска."""

    def __init__(self, path: str = "state/positions.json"):
        self.path = path
        self._data = self._load()

    def _load(self) -> dict:
        if os.path.isfile(self.path):
            with open(self.path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {"positions": {}, "consecutive_sl": {}, "last_updated": None}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._data["last_updated"] = datetime.now().isoformat()
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=2)

    def save_position(self, position: Position) -> None:
        """Сохранить/обновить позицию."""
        self._data["positions"][position.figi] = {
            "ticker": position.ticker,
            "figi": position.figi,
            "direction": position.direction.value,
            "entry_price": position.entry_price,
            "stop_price": position.stop_price,
            "target_price": position.target_price,
            "qty": position.qty,
            "entry_time": position.entry_time.isoformat(),
            "entry_reason": position.entry_reason,
            "entry_order_id": position.entry_order_id,
            "sl_order_id": position.sl_order_id,
            "tp_order_id": position.tp_order_id,
            "status": position.status,
            "candles_held": position.candles_held,
            "pending_candles": position.pending_candles,
            "last_candle_time": position.last_candle_time.isoformat() if position.last_candle_time else None,
        }
        self._save()

    def remove_position(self, figi: str) -> None:
        """Удалить позицию."""
        self._data["positions"].pop(figi, None)
        self._save()

    def get_positions(self) -> dict[str, Position]:
        """Загрузить все позиции."""
        result = {}
        for figi, raw in self._data["positions"].items():
            result[figi] = Position(
                ticker=raw["ticker"],
                figi=raw["figi"],
                direction=Signal(raw["direction"]),
                entry_price=raw["entry_price"],
                stop_price=raw["stop_price"],
                target_price=raw["target_price"],
                qty=raw["qty"],
                entry_time=datetime.fromisoformat(raw["entry_time"]),
                entry_reason=raw["entry_reason"],
                entry_order_id=raw["entry_order_id"],
                sl_order_id=raw["sl_order_id"],
                tp_order_id=raw["tp_order_id"],
                status=raw.get("status", "active"),
                candles_held=raw.get("candles_held", 0),
                pending_candles=raw.get("pending_candles", 0),
                last_candle_time=datetime.fromisoformat(raw["last_candle_time"]) if raw.get("last_candle_time") else None,
            )
        return result

    def update_candles_held(self, figi: str, candles_held: int, last_candle_time: datetime) -> None:
        """Обновить счётчик свечей и время последней свечи для позиции."""
        if figi in self._data["positions"]:
            self._data["positions"][figi]["candles_held"] = candles_held
            self._data["positions"][figi]["last_candle_time"] = last_candle_time.isoformat()
            self._save()

    def increment_consecutive_sl(self, ticker: str) -> None:
        """Увеличить счётчик подряд SL для тикера."""
        sl_data = self._data["consecutive_sl"]
        if ticker not in sl_data:
            sl_data[ticker] = {"count": 0, "last_sl_date": None}
        sl_data[ticker]["count"] += 1
        sl_data[ticker]["last_sl_date"] = date.today().isoformat()
        self._save()

    def reset_consecutive_sl(self, ticker: str) -> None:
        """Сбросить счётчик подряд SL (после TP или timeout)."""
        sl_data = self._data["consecutive_sl"]
        if ticker in sl_data:
            sl_data[ticker] = {"count": 0, "last_sl_date": None}
            self._save()

    def get_consecutive_sl(self, ticker: str) -> tuple[int, str | None]:
        """Получить (count, last_sl_date) для тикера."""
        sl_data = self._data["consecutive_sl"].get(ticker, {})
        return sl_data.get("count", 0), sl_data.get("last_sl_date")

    def reset_stale_sl_counters(self) -> None:
        """Сбросить счётчики SL, у которых last_sl_date < сегодня."""
        today = date.today().isoformat()
        for ticker, sl_data in self._data["consecutive_sl"].items():
            if sl_data.get("last_sl_date") and sl_data["last_sl_date"] < today:
                sl_data["count"] = 0
                sl_data["last_sl_date"] = None
        self._save()
