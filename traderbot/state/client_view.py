"""Адаптер per-client над SqliteStateStore.

`ExecutionManager` был написан для single-client режима: он вызывает
`self.state.save_position(position)` без указания client_id. Чтобы не
переписывать весь ExecutionManager, оборачиваем `SqliteStateStore` в
тонкий адаптер, который подставляет `client_id` автоматически.

Это решение описано в плане (фаза 2): «ClientStateView — тонкий адаптер,
подсовывающий client_id в SqliteStateStore. Имеет интерфейс старого
StateStore, чтобы ExecutionManager его вообще не заметил.»
"""
from __future__ import annotations

from datetime import datetime

from traderbot.state.sqlite_store import SqliteStateStore
from traderbot.types import Position


class ClientStateView:
    """Прокси: тот же интерфейс, что у старого JSON-StateStore, но данные
    идут в SQLite для конкретного client_id."""

    def __init__(self, store: SqliteStateStore, client_id: int):
        self._store = store
        self._client_id = client_id

    @property
    def client_id(self) -> int:
        return self._client_id

    def save_position(self, position: Position) -> None:
        self._store.save_position(self._client_id, position)

    def remove_position(self, figi: str) -> None:
        self._store.remove_position(self._client_id, figi)

    def get_positions(self) -> dict[str, Position]:
        return self._store.get_positions(self._client_id)

    def update_candles_held(self, figi: str, candles_held: int, last_candle_time: datetime) -> None:
        self._store.update_candles_held(self._client_id, figi, candles_held, last_candle_time)

    def increment_consecutive_sl(self, ticker: str) -> None:
        self._store.increment_consecutive_sl(self._client_id, ticker)

    def reset_consecutive_sl(self, ticker: str) -> None:
        self._store.reset_consecutive_sl(self._client_id, ticker)

    def get_consecutive_sl(self, ticker: str) -> tuple[int, str | None]:
        return self._store.get_consecutive_sl(self._client_id, ticker)

    def reset_stale_sl_counters(self) -> None:
        self._store.reset_stale_sl_counters(self._client_id)
