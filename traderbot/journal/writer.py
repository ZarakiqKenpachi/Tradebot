import csv
import logging
import os

import pandas as pd

from traderbot.types import TradeRecord

logger = logging.getLogger(__name__)

_CSV_COLUMNS = [
    "ticker", "figi", "direction", "entry_price", "exit_price",
    "stop_price", "target_price", "qty", "pnl", "commission",
    "entry_time", "exit_time", "entry_reason", "exit_reason", "candles_held",
]


class TradeJournal:
    """Запись сделок в CSV-файл."""

    def __init__(self, path: str = "journal/trades.csv"):
        self.path = path

    def log_trade(self, record: TradeRecord) -> None:
        """Записать сделку в CSV."""
        file_exists = os.path.isfile(self.path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)

        with open(self.path, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow(_CSV_COLUMNS)
            writer.writerow([
                record.ticker,
                record.figi,
                record.direction.value,
                record.entry_price,
                record.exit_price,
                record.stop_price,
                record.target_price,
                record.qty,
                round(record.pnl, 2),
                round(record.commission, 2),
                record.entry_time.isoformat(),
                record.exit_time.isoformat(),
                record.entry_reason,
                record.exit_reason,
                record.candles_held,
            ])

        logger.info("[JOURNAL] %s %s | pnl=%.2f",
                     record.direction.value, record.ticker, record.pnl)

    def read_trades(self) -> pd.DataFrame:
        """Прочитать все сделки из CSV в DataFrame."""
        if not os.path.isfile(self.path):
            return pd.DataFrame(columns=_CSV_COLUMNS)
        return pd.read_csv(self.path)
