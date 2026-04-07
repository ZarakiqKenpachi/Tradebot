"""Запись сделок в таблицу `trades` SQLite (per-client)."""
from __future__ import annotations

import logging

from traderbot.clients.db import Database
from traderbot.types import TradeRecord

logger = logging.getLogger(__name__)


class SqliteTradeJournal:
    """Writer, который пишет TradeRecord в таблицу `trades` с client_id."""

    def __init__(self, db: Database):
        self.db = db

    def log_trade(self, client_id: int, record: TradeRecord) -> None:
        with self.db.write() as cur:
            cur.execute(
                """
                INSERT INTO trades (
                    client_id, ticker, figi, direction,
                    entry_price, exit_price, stop_price, target_price,
                    qty, pnl, commission,
                    entry_time, exit_time, entry_reason, exit_reason, candles_held
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    client_id,
                    record.ticker,
                    record.figi,
                    record.direction.value,
                    record.entry_price,
                    record.exit_price,
                    record.stop_price,
                    record.target_price,
                    record.qty,
                    round(record.pnl, 4),
                    round(record.commission, 4),
                    record.entry_time.isoformat(),
                    record.exit_time.isoformat(),
                    record.entry_reason,
                    record.exit_reason,
                    record.candles_held,
                ),
            )
        logger.info("[JOURNAL-SQLITE] client=%d %s %s pnl=%.2f",
                    client_id, record.direction.value, record.ticker, record.pnl)
