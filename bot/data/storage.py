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

    MOEX_OPEN_MSK = 7   # Regular session starts 07:00 MSK
    MOEX_CLOSE_MSK = 24  # Last candle at 23:30 MSK

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

    def filter_moex_hours(self, df: pd.DataFrame) -> pd.DataFrame:
        """Keep only candles within MOEX regular weekday session (07:00-23:59 MSK, Mon-Fri)."""
        msk = df.index.tz_convert("Europe/Moscow")
        in_hours = (msk.hour >= self.MOEX_OPEN_MSK) & (msk.hour < self.MOEX_CLOSE_MSK)
        weekday = msk.weekday < 5
        return df[in_hours & weekday]
