"""Local SQLite cache for candle data — avoid re-fetching from TV."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

logger = logging.getLogger(__name__)


class CandleCache:
    """SQLite-backed candle cache.

    Stores candles keyed by (symbol, exchange, timeframe).
    On fetch, returns cached data if fresh enough, otherwise returns empty.
    """

    def __init__(self, db_path: str = "data/chart_cache.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS candles (
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                timestamp TEXT NOT NULL,
                open REAL NOT NULL,
                high REAL NOT NULL,
                low REAL NOT NULL,
                close REAL NOT NULL,
                volume REAL NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, exchange, timeframe, timestamp)
            );

            CREATE TABLE IF NOT EXISTS cache_meta (
                symbol TEXT NOT NULL,
                exchange TEXT NOT NULL,
                timeframe TEXT NOT NULL,
                last_update TEXT NOT NULL,
                bar_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (symbol, exchange, timeframe)
            );

            CREATE INDEX IF NOT EXISTS idx_candles_lookup
                ON candles (symbol, exchange, timeframe, timestamp);
        """)
        self._conn.commit()

    def get(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        max_age_seconds: int = 300,
    ) -> pd.DataFrame | None:
        """Get cached candles if they exist and are fresh enough.

        Returns None if cache is stale or missing.
        """
        row = self._conn.execute(
            "SELECT last_update FROM cache_meta WHERE symbol=? AND exchange=? AND timeframe=?",
            (symbol, exchange, timeframe),
        ).fetchone()

        if row is None:
            return None

        last_update = datetime.fromisoformat(row[0])
        age = (datetime.now(timezone.utc) - last_update).total_seconds()
        if age > max_age_seconds:
            return None

        rows = self._conn.execute(
            """SELECT timestamp, open, high, low, close, volume
               FROM candles
               WHERE symbol=? AND exchange=? AND timeframe=?
               ORDER BY timestamp""",
            (symbol, exchange, timeframe),
        ).fetchall()

        if not rows:
            return None

        df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")
        return df

    def put(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        df: pd.DataFrame,
    ) -> None:
        """Store candles in cache, replacing existing data for this key."""
        if df.empty:
            return

        key = (symbol, exchange, timeframe)

        # Clear old data for this key
        self._conn.execute(
            "DELETE FROM candles WHERE symbol=? AND exchange=? AND timeframe=?", key
        )

        # Insert new data
        records = []
        for ts, row in df.iterrows():
            records.append((
                symbol, exchange, timeframe,
                ts.isoformat(),
                float(row["open"]), float(row["high"]),
                float(row["low"]), float(row["close"]),
                float(row.get("volume", 0)),
            ))

        self._conn.executemany(
            """INSERT OR REPLACE INTO candles
               (symbol, exchange, timeframe, timestamp, open, high, low, close, volume)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            records,
        )

        # Update meta
        self._conn.execute(
            """INSERT OR REPLACE INTO cache_meta (symbol, exchange, timeframe, last_update, bar_count)
               VALUES (?, ?, ?, ?, ?)""",
            (*key, datetime.now(timezone.utc).isoformat(), len(df)),
        )
        self._conn.commit()
        logger.debug("[CACHE] Stored %d bars for %s:%s %s", len(df), exchange, symbol, timeframe)

    def invalidate(self, symbol: str, exchange: str, timeframe: str) -> None:
        """Remove cached data for a specific key."""
        key = (symbol, exchange, timeframe)
        self._conn.execute(
            "DELETE FROM candles WHERE symbol=? AND exchange=? AND timeframe=?", key
        )
        self._conn.execute(
            "DELETE FROM cache_meta WHERE symbol=? AND exchange=? AND timeframe=?", key
        )
        self._conn.commit()

    def clear_all(self) -> None:
        """Clear entire cache."""
        self._conn.execute("DELETE FROM candles")
        self._conn.execute("DELETE FROM cache_meta")
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
