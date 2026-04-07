"""Агрегирующий journal: пишет в несколько writer-ов параллельно.

Используется для одновременной записи сделок в SQLite (per-client таблица
`trades`) и в CSV-файл `journal/trades.csv` (общий лог для админа).

Per-client view (`ClientJournalView`) прокидывает client_id и имеет
интерфейс `log_trade(record)`, совместимый со старым `TradeJournal`,
чтобы `ExecutionManager` не пришлось менять.
"""
from __future__ import annotations

import logging

from traderbot.journal.sqlite_writer import SqliteTradeJournal
from traderbot.journal.writer import TradeJournal
from traderbot.types import TradeRecord

logger = logging.getLogger(__name__)


class MultiTradeJournal:
    """Пишет каждую сделку в SQLite и в CSV. Ошибка в одном writer не
    мешает другому (только логируется)."""

    def __init__(self, sqlite_journal: SqliteTradeJournal, csv_journal: TradeJournal | None = None):
        self.sqlite = sqlite_journal
        self.csv = csv_journal

    def log_trade(self, client_id: int, record: TradeRecord) -> None:
        try:
            self.sqlite.log_trade(client_id, record)
        except Exception:
            logger.exception("[JOURNAL] SQLite write failed for client=%d %s",
                             client_id, record.ticker)
        if self.csv is not None:
            try:
                self.csv.log_trade(record)
            except Exception:
                logger.exception("[JOURNAL] CSV write failed for client=%d %s",
                                 client_id, record.ticker)


class ClientJournalView:
    """Адаптер per-client, чтобы `ExecutionManager` мог вызывать старый
    интерфейс `log_trade(record)`."""

    def __init__(self, multi: MultiTradeJournal, client_id: int):
        self._multi = multi
        self._client_id = client_id

    @property
    def client_id(self) -> int:
        return self._client_id

    def log_trade(self, record: TradeRecord) -> None:
        self._multi.log_trade(self._client_id, record)
