"""Per-client хранение позиций и счётчиков SL в SQLite.

Заменяет старый `traderbot.state.store.StateStore` (JSON-файл) для
multi-tenant режима. Все методы принимают `client_id: int` первым
аргументом — один экземпляр `SqliteStateStore` обслуживает всех клиентов.

Для совместимости с `ExecutionManager`, который ожидает старый интерфейс
`StateStore`, используется адаптер `ClientStateView` (см. client_view.py),
прокидывающий `client_id` прозрачно.
"""
from __future__ import annotations

import logging
from datetime import date, datetime

from traderbot.clients.db import Database
from traderbot.types import Position, Signal

logger = logging.getLogger(__name__)


class SqliteStateStore:
    """Thread-safe хранение позиций в SQLite.

    Схема таблиц `positions` и `consecutive_sl` определена в
    `traderbot.clients.db` (init_schema). Этот класс не создаёт таблицы,
    только читает/пишет.
    """

    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    # Позиции
    # ------------------------------------------------------------------

    def save_position(self, client_id: int, position: Position) -> None:
        """Сохранить/обновить позицию (upsert по (client_id, figi))."""
        last_candle = position.last_candle_time.isoformat() if position.last_candle_time else None
        with self.db.write() as cur:
            cur.execute(
                """
                INSERT INTO positions (
                    client_id, figi, ticker, direction,
                    entry_price, stop_price, target_price,
                    qty, lot_size, entry_time, entry_reason,
                    entry_order_id, sl_order_id, tp_order_id,
                    status, candles_held, pending_candles, last_candle_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(client_id, figi) DO UPDATE SET
                    ticker = excluded.ticker,
                    direction = excluded.direction,
                    entry_price = excluded.entry_price,
                    stop_price = excluded.stop_price,
                    target_price = excluded.target_price,
                    qty = excluded.qty,
                    lot_size = excluded.lot_size,
                    entry_time = excluded.entry_time,
                    entry_reason = excluded.entry_reason,
                    entry_order_id = excluded.entry_order_id,
                    sl_order_id = excluded.sl_order_id,
                    tp_order_id = excluded.tp_order_id,
                    status = excluded.status,
                    candles_held = excluded.candles_held,
                    pending_candles = excluded.pending_candles,
                    last_candle_time = excluded.last_candle_time
                """,
                (
                    client_id,
                    position.figi,
                    position.ticker,
                    position.direction.value,
                    position.entry_price,
                    position.stop_price,
                    position.target_price,
                    position.qty,
                    position.lot_size,
                    position.entry_time.isoformat(),
                    position.entry_reason,
                    position.entry_order_id,
                    position.sl_order_id,
                    position.tp_order_id,
                    position.status,
                    position.candles_held,
                    position.pending_candles,
                    last_candle,
                ),
            )

    def remove_position(self, client_id: int, figi: str) -> None:
        with self.db.write() as cur:
            cur.execute(
                "DELETE FROM positions WHERE client_id = ? AND figi = ?",
                (client_id, figi),
            )

    def get_positions(self, client_id: int) -> dict[str, Position]:
        """Загрузить все позиции клиента как figi → Position."""
        with self.db.cursor() as cur:
            cur.execute("SELECT * FROM positions WHERE client_id = ?", (client_id,))
            rows = cur.fetchall()
        result: dict[str, Position] = {}
        for r in rows:
            result[r["figi"]] = Position(
                ticker=r["ticker"],
                figi=r["figi"],
                direction=Signal(r["direction"]),
                entry_price=r["entry_price"],
                stop_price=r["stop_price"],
                target_price=r["target_price"],
                qty=r["qty"],
                lot_size=r["lot_size"],
                entry_time=datetime.fromisoformat(r["entry_time"]),
                entry_reason=r["entry_reason"] or "",
                entry_order_id=r["entry_order_id"] or "",
                sl_order_id=r["sl_order_id"] or "",
                tp_order_id=r["tp_order_id"] or "",
                status=r["status"],
                candles_held=r["candles_held"],
                pending_candles=r["pending_candles"],
                last_candle_time=datetime.fromisoformat(r["last_candle_time"])
                    if r["last_candle_time"] else None,
            )
        return result

    def update_candles_held(
        self, client_id: int, figi: str, candles_held: int, last_candle_time: datetime
    ) -> None:
        with self.db.write() as cur:
            cur.execute(
                """
                UPDATE positions
                SET candles_held = ?, last_candle_time = ?
                WHERE client_id = ? AND figi = ?
                """,
                (candles_held, last_candle_time.isoformat(), client_id, figi),
            )

    # ------------------------------------------------------------------
    # Consecutive SL counters
    # ------------------------------------------------------------------

    def increment_consecutive_sl(self, client_id: int, ticker: str) -> None:
        today = date.today().isoformat()
        with self.db.write() as cur:
            cur.execute(
                """
                INSERT INTO consecutive_sl (client_id, ticker, count, last_sl_date)
                VALUES (?, ?, 1, ?)
                ON CONFLICT(client_id, ticker) DO UPDATE SET
                    count = count + 1,
                    last_sl_date = excluded.last_sl_date
                """,
                (client_id, ticker, today),
            )

    def reset_consecutive_sl(self, client_id: int, ticker: str) -> None:
        with self.db.write() as cur:
            cur.execute(
                """
                UPDATE consecutive_sl
                SET count = 0, last_sl_date = NULL
                WHERE client_id = ? AND ticker = ?
                """,
                (client_id, ticker),
            )

    def get_consecutive_sl(self, client_id: int, ticker: str) -> tuple[int, str | None]:
        with self.db.cursor() as cur:
            cur.execute(
                "SELECT count, last_sl_date FROM consecutive_sl WHERE client_id = ? AND ticker = ?",
                (client_id, ticker),
            )
            row = cur.fetchone()
        if row is None:
            return 0, None
        return int(row["count"]), row["last_sl_date"]

    def reset_stale_sl_counters(self, client_id: int) -> None:
        """Сбросить счётчики SL у тикеров, где last_sl_date < сегодня."""
        today = date.today().isoformat()
        with self.db.write() as cur:
            cur.execute(
                """
                UPDATE consecutive_sl
                SET count = 0, last_sl_date = NULL
                WHERE client_id = ?
                  AND last_sl_date IS NOT NULL
                  AND last_sl_date < ?
                """,
                (client_id, today),
            )
