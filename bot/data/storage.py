import sqlite3

import pandas as pd


class MarketDataStorage:
    def __init__(self, db_path: str = "data.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS candles (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticker TEXT NOT NULL,
                    time TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    volume INTEGER
                )
            """)

    def save_candles(self, ticker: str, candles: list[dict]):
        with sqlite3.connect(self.db_path) as conn:
            conn.executemany(
                "INSERT INTO candles (ticker, time, open, high, low, close, volume) "
                "VALUES (:ticker, :time, :open, :high, :low, :close, :volume)",
                [{"ticker": ticker, **c} for c in candles],
            )

    def get_candles(self, ticker: str) -> list[dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM candles WHERE ticker = ? ORDER BY time", (ticker,)
            ).fetchall()
        return [dict(row) for row in rows]

    def resample(self, df_1m: pd.DataFrame, freq: str) -> pd.DataFrame:
        """Resample 1m OHLCV dataframe to a higher timeframe (e.g. '30min', '1h')."""
        resampled = df_1m.resample(freq).agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        ).dropna()
        return resampled
