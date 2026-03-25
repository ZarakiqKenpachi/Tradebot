import sqlite3


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
