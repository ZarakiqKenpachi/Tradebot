"""Persistent storage for simulation results — SQLite."""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from traderbot.chart.trades.models import TradeDisplayRecord

logger = logging.getLogger(__name__)


class SimulationJournal:
    """Save and load simulation run results to SQLite.

    Each run is stored as a session with metadata + individual trades.
    This allows reviewing past simulations and comparing strategy changes.
    """

    def __init__(self, db_path: str = "data/sim_journal.db"):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self._db_path), check_same_thread=False)
        self._init_tables()

    def _init_tables(self) -> None:
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS sim_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                strategy TEXT NOT NULL,
                ticker TEXT NOT NULL,
                exchange TEXT NOT NULL DEFAULT '',
                timeframe TEXT NOT NULL DEFAULT '',
                scan_tf TEXT NOT NULL DEFAULT '',
                initial_balance REAL NOT NULL DEFAULT 0,
                final_balance REAL NOT NULL DEFAULT 0,
                total_pnl REAL NOT NULL DEFAULT 0,
                max_drawdown REAL NOT NULL DEFAULT 0,
                setups_found INTEGER NOT NULL DEFAULT 0,
                trade_count INTEGER NOT NULL DEFAULT 0,
                win_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS sim_trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id INTEGER NOT NULL,
                ticker TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry_price REAL NOT NULL,
                exit_price REAL NOT NULL,
                stop_price REAL NOT NULL,
                target_price REAL NOT NULL,
                qty INTEGER NOT NULL DEFAULT 0,
                pnl REAL NOT NULL DEFAULT 0,
                commission REAL NOT NULL DEFAULT 0,
                entry_time TEXT NOT NULL,
                exit_time TEXT NOT NULL,
                entry_reason TEXT NOT NULL DEFAULT '',
                exit_reason TEXT NOT NULL DEFAULT '',
                candles_held INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (run_id) REFERENCES sim_runs(id)
            );

            CREATE INDEX IF NOT EXISTS idx_sim_trades_run
                ON sim_trades (run_id);
            CREATE INDEX IF NOT EXISTS idx_sim_runs_strategy
                ON sim_runs (strategy, ticker);
        """)
        self._conn.commit()

    def save_run(
        self,
        strategy: str,
        ticker: str,
        exchange: str,
        timeframe: str,
        scan_tf: str,
        initial_balance: float,
        final_balance: float,
        total_pnl: float,
        max_drawdown: float,
        setups_found: int,
        trades: list[TradeDisplayRecord],
    ) -> int:
        """Save a simulation run. Returns the run_id."""
        now = datetime.now(timezone.utc).isoformat()
        wins = sum(1 for t in trades if t.pnl > 0)

        cur = self._conn.execute(
            """INSERT INTO sim_runs
               (strategy, ticker, exchange, timeframe, scan_tf,
                initial_balance, final_balance, total_pnl, max_drawdown,
                setups_found, trade_count, win_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (strategy, ticker, exchange, timeframe, scan_tf,
             initial_balance, final_balance, round(total_pnl, 4),
             round(max_drawdown, 6), setups_found, len(trades), wins, now),
        )
        run_id = cur.lastrowid

        for t in trades:
            self._conn.execute(
                """INSERT INTO sim_trades
                   (run_id, ticker, direction, entry_price, exit_price,
                    stop_price, target_price, qty, pnl, commission,
                    entry_time, exit_time, entry_reason, exit_reason, candles_held)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, t.ticker, t.direction, t.entry_price, t.exit_price,
                 t.stop_price, t.target_price, t.qty,
                 round(t.pnl, 4), round(t.commission, 4),
                 t.entry_time, t.exit_time,
                 t.entry_reason, t.exit_reason, t.candles_held),
            )

        self._conn.commit()
        logger.info(
            "[SIM-JOURNAL] Saved run #%d: %s on %s, %d trades, P&L=%.2f",
            run_id, strategy, ticker, len(trades), total_pnl,
        )
        return run_id

    def get_runs(
        self, strategy: str | None = None, ticker: str | None = None, limit: int = 50,
    ) -> list[dict]:
        """Get list of past simulation runs."""
        query = "SELECT * FROM sim_runs WHERE 1=1"
        params: list = []
        if strategy:
            query += " AND strategy = ?"
            params.append(strategy)
        if ticker:
            query += " AND ticker = ?"
            params.append(ticker)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        rows = self._conn.execute(query, params).fetchall()
        cols = [d[0] for d in self._conn.execute("SELECT * FROM sim_runs LIMIT 0").description]
        return [dict(zip(cols, row)) for row in rows]

    def get_run_trades(self, run_id: int) -> list[TradeDisplayRecord]:
        """Get trades for a specific run."""
        rows = self._conn.execute(
            """SELECT id, 0 as client_id, ticker, '' as figi, direction,
                      entry_price, exit_price, stop_price, target_price,
                      qty, pnl, commission,
                      entry_time, exit_time, entry_reason, exit_reason, candles_held
               FROM sim_trades WHERE run_id = ? ORDER BY entry_time""",
            (run_id,),
        ).fetchall()
        return [TradeDisplayRecord.from_db_row(row) for row in rows]

    def delete_run(self, run_id: int) -> None:
        """Delete a simulation run and its trades."""
        self._conn.execute("DELETE FROM sim_trades WHERE run_id = ?", (run_id,))
        self._conn.execute("DELETE FROM sim_runs WHERE id = ?", (run_id,))
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
