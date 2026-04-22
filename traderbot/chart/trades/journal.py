"""Read trades from SQLite database and CSV journal files."""
from __future__ import annotations

import csv
import logging
import sqlite3
from pathlib import Path

from traderbot.chart.trades.models import TradeDisplayRecord

logger = logging.getLogger(__name__)


class TradeJournalReader:
    """Read trades from the bot's SQLite database or CSV files."""

    def __init__(self, db_path: str | None = None):
        self._db_path = db_path

    def read_from_sqlite(
        self,
        ticker: str | None = None,
        client_id: int | None = None,
        limit: int = 500,
    ) -> list[TradeDisplayRecord]:
        """Read trades from SQLite trades table."""
        if not self._db_path or not Path(self._db_path).exists():
            logger.warning("[JOURNAL] Database not found: %s", self._db_path)
            return []

        conn = sqlite3.connect(self._db_path)
        try:
            query = """
                SELECT id, client_id, ticker, figi, direction,
                       entry_price, exit_price, stop_price, target_price,
                       qty, pnl, commission,
                       entry_time, exit_time, entry_reason, exit_reason, candles_held
                FROM trades
                WHERE 1=1
            """
            params: list = []

            if ticker:
                query += " AND ticker = ?"
                params.append(ticker)
            if client_id is not None:
                query += " AND client_id = ?"
                params.append(client_id)

            query += " ORDER BY exit_time DESC LIMIT ?"
            params.append(limit)

            rows = conn.execute(query, params).fetchall()
            return [TradeDisplayRecord.from_db_row(row) for row in rows]
        except Exception:
            logger.exception("[JOURNAL] Failed to read trades from SQLite")
            return []
        finally:
            conn.close()

    def read_from_csv(self, csv_path: str, ticker: str | None = None) -> list[TradeDisplayRecord]:
        """Read trades from CSV journal file."""
        path = Path(csv_path)
        if not path.exists():
            return []

        records = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for i, row in enumerate(reader):
                    if ticker and row.get("ticker") != ticker:
                        continue
                    records.append(TradeDisplayRecord(
                        id=i,
                        ticker=row.get("ticker", ""),
                        figi=row.get("figi", ""),
                        direction=row.get("direction", ""),
                        entry_price=float(row.get("entry_price", 0)),
                        exit_price=float(row.get("exit_price", 0)),
                        stop_price=float(row.get("stop_price", 0)),
                        target_price=float(row.get("target_price", 0)),
                        qty=int(row.get("qty", 0)),
                        pnl=float(row.get("pnl", 0)),
                        commission=float(row.get("commission", 0)),
                        entry_time=row.get("entry_time", ""),
                        exit_time=row.get("exit_time", ""),
                        entry_reason=row.get("entry_reason", ""),
                        exit_reason=row.get("exit_reason", ""),
                        candles_held=int(row.get("candles_held", 0)),
                    ))
        except Exception:
            logger.exception("[JOURNAL] Failed to read CSV: %s", csv_path)

        return records
