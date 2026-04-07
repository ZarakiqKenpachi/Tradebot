"""Self-test Фазы 2: SqliteStateStore + ClientStateView + SqliteTradeJournal + MultiTradeJournal.

Запуск: py -3.12 -m traderbot.state
"""
from __future__ import annotations

import logging
import os
import tempfile
from datetime import datetime, timezone

from traderbot.clients.db import Database
from traderbot.clients.models import ClientRole, ClientStatus
from traderbot.clients.registry import ClientRegistry
from traderbot.journal.multi_writer import ClientJournalView, MultiTradeJournal
from traderbot.journal.sqlite_writer import SqliteTradeJournal
from traderbot.journal.writer import TradeJournal
from traderbot.state.client_view import ClientStateView
from traderbot.state.sqlite_store import SqliteStateStore
from traderbot.types import Position, Signal, TradeRecord


def _make_position(ticker: str, figi: str) -> Position:
    return Position(
        ticker=ticker,
        figi=figi,
        direction=Signal.BUY,
        entry_price=280.5,
        stop_price=275.0,
        target_price=290.0,
        qty=10,
        lot_size=10,
        entry_time=datetime(2026, 4, 6, 10, 30, tzinfo=timezone.utc),
        entry_reason="test setup",
        entry_order_id="order-123",
        sl_order_id="sl-456",
        tp_order_id="tp-789",
        status="active",
        candles_held=3,
        pending_candles=0,
        last_candle_time=datetime(2026, 4, 6, 12, 0, tzinfo=timezone.utc),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")
        csv_path = os.path.join(tmp, "trades.csv")

        db = Database(db_path)
        db.init_schema()
        reg = ClientRegistry(db)

        # Создать двух клиентов
        c1 = reg.create(tg_chat_id=100, role=ClientRole.ADMIN, status=ClientStatus.ACTIVE,
                        tbank_token="t.admin", tbank_account_id="a1")
        c2 = reg.create(tg_chat_id=200, role=ClientRole.SUBSCRIBER, status=ClientStatus.ACTIVE,
                        tbank_token="t.sub", tbank_account_id="a2")


        # State store + per-client views
        state = SqliteStateStore(db)
        view1 = ClientStateView(state, c1.id)
        view2 = ClientStateView(state, c2.id)

        # 1. Каждый клиент видит свои позиции независимо
        p_sber = _make_position("SBER", "BBG004730N88")
        p_gazp = _make_position("GAZP", "BBG004730RP0")
        view1.save_position(p_sber)
        view1.save_position(p_gazp)
        view2.save_position(p_sber)  # тот же figi, но для другого клиента

        positions1 = view1.get_positions()
        positions2 = view2.get_positions()
        assert len(positions1) == 2, f"client1 expected 2 positions, got {len(positions1)}"
        assert len(positions2) == 1, f"client2 expected 1 position, got {len(positions2)}"
        assert "BBG004730N88" in positions1 and "BBG004730RP0" in positions1
        assert "BBG004730N88" in positions2 and "BBG004730RP0" not in positions2
        print("OK per-client position isolation")

        # 2. round-trip позиции — все поля совпадают
        restored = positions1["BBG004730N88"]
        assert restored.ticker == "SBER"
        assert restored.direction == Signal.BUY
        assert restored.entry_price == 280.5
        assert restored.qty == 10
        assert restored.lot_size == 10
        assert restored.status == "active"
        assert restored.candles_held == 3
        assert restored.entry_order_id == "order-123"
        assert restored.sl_order_id == "sl-456"
        assert restored.tp_order_id == "tp-789"
        assert restored.last_candle_time is not None
        assert restored.last_candle_time.year == 2026
        print("OK position round-trip preserves all fields")

        # 3. Upsert (save_position повторно) не плодит дубликаты
        p_sber_updated = _make_position("SBER", "BBG004730N88")
        p_sber_updated.candles_held = 7
        view1.save_position(p_sber_updated)
        positions1 = view1.get_positions()
        assert len(positions1) == 2  # всё ещё 2, не 3
        assert positions1["BBG004730N88"].candles_held == 7
        print("OK save_position is upsert")

        # 4. update_candles_held
        new_time = datetime(2026, 4, 6, 14, 0, tzinfo=timezone.utc)
        view1.update_candles_held("BBG004730N88", 8, new_time)
        assert view1.get_positions()["BBG004730N88"].candles_held == 8
        print("OK update_candles_held")

        # 5. remove_position
        view1.remove_position("BBG004730RP0")
        positions1 = view1.get_positions()
        assert "BBG004730RP0" not in positions1
        assert "BBG004730N88" in positions1
        print("OK remove_position")

        # 6. consecutive_sl изоляция между клиентами
        view1.increment_consecutive_sl("SBER")
        view1.increment_consecutive_sl("SBER")
        view1.increment_consecutive_sl("SBER")
        view2.increment_consecutive_sl("SBER")
        c1_sl, _ = view1.get_consecutive_sl("SBER")
        c2_sl, _ = view2.get_consecutive_sl("SBER")
        assert c1_sl == 3, f"expected 3 for client1, got {c1_sl}"
        assert c2_sl == 1, f"expected 1 for client2, got {c2_sl}"
        print("OK consecutive_sl per-client isolation")

        view1.reset_consecutive_sl("SBER")
        c1_sl, _ = view1.get_consecutive_sl("SBER")
        c2_sl, _ = view2.get_consecutive_sl("SBER")
        assert c1_sl == 0
        assert c2_sl == 1  # client2 не затронут
        print("OK reset_consecutive_sl is client-scoped")

        # 7. TradeJournal: Sqlite + CSV multi-writer
        sqlite_journal = SqliteTradeJournal(db)
        csv_journal = TradeJournal(csv_path)
        multi = MultiTradeJournal(sqlite_journal, csv_journal)
        j1 = ClientJournalView(multi, c1.id)
        j2 = ClientJournalView(multi, c2.id)

        rec = TradeRecord(
            ticker="SBER",
            figi="BBG004730N88",
            direction=Signal.BUY,
            entry_price=280.5,
            exit_price=290.0,
            stop_price=275.0,
            target_price=290.0,
            qty=10,
            pnl=950.0,
            commission=23.0,
            entry_time=datetime(2026, 4, 6, 10, 30, tzinfo=timezone.utc),
            exit_time=datetime(2026, 4, 6, 15, 0, tzinfo=timezone.utc),
            entry_reason="sweep",
            exit_reason="take_profit",
            candles_held=9,
        )
        j1.log_trade(rec)
        j2.log_trade(rec)
        j1.log_trade(rec)  # ещё одна для c1

        with db.cursor() as cur:
            cur.execute("SELECT client_id, COUNT(*) AS n FROM trades GROUP BY client_id ORDER BY client_id")
            counts = {row["client_id"]: row["n"] for row in cur.fetchall()}
        assert counts[c1.id] == 2, f"client1 trades: {counts}"
        assert counts[c2.id] == 1, f"client2 trades: {counts}"
        print("OK SqliteTradeJournal per-client counts")

        assert os.path.isfile(csv_path), "CSV journal file not created"
        with open(csv_path, "r", encoding="utf-8") as f:
            csv_lines = f.readlines()
        # header + 3 trades
        assert len(csv_lines) == 4, f"expected 4 lines in CSV, got {len(csv_lines)}"
        print("OK CSV writer still works in parallel")

        db.close()
    print("\nAll phase-2 self-tests passed.")


if __name__ == "__main__":
    main()
