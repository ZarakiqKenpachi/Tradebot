"""Trade data models for the GUI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class TradeDisplayRecord:
    """Trade record for display in the GUI table and chart markers."""
    id: int | None = None
    client_id: int | None = None
    ticker: str = ""
    figi: str = ""
    direction: str = ""
    entry_price: float = 0.0
    exit_price: float = 0.0
    stop_price: float = 0.0
    target_price: float = 0.0
    qty: int = 0
    pnl: float = 0.0
    commission: float = 0.0
    entry_time: str = ""
    exit_time: str = ""
    entry_reason: str = ""
    exit_reason: str = ""
    candles_held: int = 0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "client_id": self.client_id,
            "ticker": self.ticker,
            "figi": self.figi,
            "direction": self.direction,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "qty": self.qty,
            "pnl": self.pnl,
            "commission": self.commission,
            "entry_time": self.entry_time,
            "exit_time": self.exit_time,
            "entry_reason": self.entry_reason,
            "exit_reason": self.exit_reason,
            "candles_held": self.candles_held,
        }

    @staticmethod
    def from_db_row(row: tuple) -> TradeDisplayRecord:
        """Create from SQLite row (id, client_id, ticker, ..., candles_held)."""
        return TradeDisplayRecord(
            id=row[0],
            client_id=row[1],
            ticker=row[2],
            figi=row[3],
            direction=row[4],
            entry_price=row[5],
            exit_price=row[6],
            stop_price=row[7],
            target_price=row[8],
            qty=row[9],
            pnl=row[10],
            commission=row[11],
            entry_time=row[12],
            exit_time=row[13],
            entry_reason=row[14],
            exit_reason=row[15],
            candles_held=row[16],
        )
