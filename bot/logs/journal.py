import csv
import os
from datetime import datetime, timezone

LOG_FILE = "trades.csv"
FIELDS = [
    "timestamp", "figi", "direction",
    "entry_price", "exit_price", "stop_price", "target_price",
    "pnl", "reason", "candles_held",
]


class TradeJournal:
    def __init__(self, path: str = LOG_FILE):
        self.path = path
        if not os.path.exists(self.path):
            with open(self.path, "w", newline="") as f:
                csv.DictWriter(f, fieldnames=FIELDS).writeheader()

    def log_trade(
        self,
        figi: str,
        direction: str,
        entry_price: float,
        exit_price: float,
        stop_price: float,
        target_price: float,
        pnl: float,
        reason: str,
        candles_held: int,
    ):
        row = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "figi": figi,
            "direction": direction,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "stop_price": stop_price,
            "target_price": target_price,
            "pnl": round(pnl, 4),
            "reason": reason,
            "candles_held": candles_held,
        }
        with open(self.path, "a", newline="") as f:
            csv.DictWriter(f, fieldnames=FIELDS).writerow(row)
        print(
            f"[JOURNAL] {direction} {figi} | entry={entry_price} exit={exit_price} "
            f"pnl={row['pnl']} reason={reason} candles={candles_held}"
        )
