"""Инициализация SQLite и schema-миграции для реестра клиентов.

Использование:
    from traderbot.clients.db import Database

    db = Database("data/traderbot.db")
    db.init_schema()
    with db.cursor() as cur:
        cur.execute("SELECT * FROM clients")
        rows = cur.fetchall()

Thread-safety:
    Один процесс разделяет `Database` между main loop и Telegram polling
    thread. Коннект создаётся с `check_same_thread=False`, все write-операции
    защищены `threading.Lock`. Для read-операций снаружи используется
    тот же лок, если нужна согласованность с недавними записями.
"""
from __future__ import annotations

import logging
import os
import sqlite3
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)


CURRENT_SCHEMA_VERSION = 1


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS clients (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_chat_id         INTEGER UNIQUE NOT NULL,
    role               TEXT NOT NULL CHECK(role IN ('admin','subscriber')),
    status             TEXT NOT NULL CHECK(status IN (
                          'pending_payment','pending_email','pending_token',
                          'active','paused','expired','revoked'
                       )),
    email              TEXT,
    account_name       TEXT,
    tbank_token        TEXT,
    tbank_account_id   TEXT,
    paid_until         TEXT,
    consecutive_errors INTEGER NOT NULL DEFAULT 0,
    created_at         TEXT NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS positions (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id        INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    figi             TEXT NOT NULL,
    ticker           TEXT NOT NULL,
    direction        TEXT NOT NULL,
    entry_price      REAL NOT NULL,
    stop_price       REAL NOT NULL,
    target_price     REAL NOT NULL,
    qty              INTEGER NOT NULL,
    lot_size         INTEGER NOT NULL,
    entry_time       TEXT NOT NULL,
    entry_reason     TEXT,
    entry_order_id   TEXT,
    sl_order_id      TEXT,
    tp_order_id      TEXT,
    status           TEXT NOT NULL,
    candles_held     INTEGER NOT NULL DEFAULT 0,
    pending_candles  INTEGER NOT NULL DEFAULT 0,
    last_candle_time TEXT,
    UNIQUE(client_id, figi)
);
CREATE INDEX IF NOT EXISTS idx_positions_client ON positions(client_id);

CREATE TABLE IF NOT EXISTS consecutive_sl (
    client_id    INTEGER NOT NULL REFERENCES clients(id) ON DELETE CASCADE,
    ticker       TEXT NOT NULL,
    count        INTEGER NOT NULL DEFAULT 0,
    last_sl_date TEXT,
    PRIMARY KEY (client_id, ticker)
);

CREATE TABLE IF NOT EXISTS trades (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id     INTEGER NOT NULL REFERENCES clients(id),
    ticker        TEXT NOT NULL,
    figi          TEXT NOT NULL,
    direction     TEXT NOT NULL,
    entry_price   REAL NOT NULL,
    exit_price    REAL NOT NULL,
    stop_price    REAL NOT NULL,
    target_price  REAL NOT NULL,
    qty           INTEGER NOT NULL,
    pnl           REAL NOT NULL,
    commission    REAL NOT NULL,
    entry_time    TEXT NOT NULL,
    exit_time     TEXT NOT NULL,
    entry_reason  TEXT,
    exit_reason   TEXT,
    candles_held  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_trades_client_exit ON trades(client_id, exit_time);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id   INTEGER NOT NULL REFERENCES clients(id),
    provider    TEXT NOT NULL,
    amount_rub  REAL NOT NULL,
    status      TEXT NOT NULL,
    external_id TEXT,
    period_days INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    paid_at     TEXT
);
CREATE INDEX IF NOT EXISTS idx_payments_client ON payments(client_id);
"""


class Database:
    """Обёртка над sqlite3-коннектом с thread-safe доступом.

    Коннект открывается один раз и переиспользуется. Все write-операции
    должны выполняться внутри контекстного менеджера `self.write()` —
    он берёт внутренний `threading.Lock` и коммитит транзакцию на выходе.
    Для чтения используйте `self.cursor()` (без лока — sqlite3 сам
    сериализует читателей при `check_same_thread=False`).
    """

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def init_schema(self) -> None:
        """Создать таблицы, если их нет, и применить миграции."""
        with self.write() as cur:
            cur.executescript(_SCHEMA_V1)
            cur.execute("SELECT version FROM schema_version LIMIT 1")
            row = cur.fetchone()
            if row is None:
                cur.execute("INSERT INTO schema_version(version) VALUES (?)", (CURRENT_SCHEMA_VERSION,))
                logger.info("[DB] Schema initialized at version %d", CURRENT_SCHEMA_VERSION)
            else:
                existing = int(row["version"])
                if existing < CURRENT_SCHEMA_VERSION:
                    # Задел под будущие миграции
                    logger.info("[DB] Migrating schema from %d to %d", existing, CURRENT_SCHEMA_VERSION)
                    cur.execute("UPDATE schema_version SET version = ?", (CURRENT_SCHEMA_VERSION,))
                elif existing > CURRENT_SCHEMA_VERSION:
                    raise RuntimeError(
                        f"DB schema version {existing} is newer than code "
                        f"supports ({CURRENT_SCHEMA_VERSION}). Update the bot."
                    )

    # ------------------------------------------------------------------
    # Cursor helpers
    # ------------------------------------------------------------------

    @contextmanager
    def write(self):
        """Транзакционный курсор под локом. BEGIN/COMMIT автоматически."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                cur.execute("BEGIN")
                yield cur
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cur.close()

    @contextmanager
    def cursor(self):
        """Курсор для чтения (без явной транзакции, но под тем же локом —
        это гарантирует видимость последних коммитов из main loop)."""
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
