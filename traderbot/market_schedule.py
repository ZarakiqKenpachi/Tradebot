"""Расписание торгов MOEX.

Торговые часы: 07:00–23:50 МСК, будни.
Праздники: загружаются из T-Bank API (fallback — только будни).
"""

import logging
from datetime import date, datetime, time as dt_time, timedelta
from zoneinfo import ZoneInfo

from traderbot.broker.tbank import TBankBroker

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
_MARKET_OPEN = dt_time(7, 0)
_MARKET_CLOSE = dt_time(23, 50)


class MarketSchedule:
    """Расписание торгов: фиксированные часы + праздники из API."""

    def __init__(self, broker: TBankBroker, exchange: str):
        self._broker = broker
        self._exchange = exchange
        self._holidays: set[date] = set()
        self._last_fetch_date: date | None = None

    @classmethod
    def from_figi(cls, broker: TBankBroker, figi: str) -> "MarketSchedule":
        """Создать MarketSchedule, определив биржу из инструмента."""
        try:
            exchange = broker.get_instrument_exchange(figi)
            logger.info("[SCHEDULE] Биржа определена из инструмента: %s", exchange)
        except Exception:
            exchange = "MOEX"
            logger.warning("[SCHEDULE] Не удалось определить биржу, используем %s", exchange)
        return cls(broker, exchange)

    def refresh(self) -> None:
        """Загрузить праздники на 7 дней вперёд (не чаще раза в сутки)."""
        today = datetime.now(MSK).date()
        if self._last_fetch_date == today:
            return
        try:
            schedule = self._broker.get_trading_schedule(
                exchange=self._exchange,
                from_date=today,
                to_date=today + timedelta(days=7),
            )
            for d, info in schedule.items():
                if not info.is_trading_day:
                    self._holidays.add(d)
                else:
                    self._holidays.discard(d)
            self._last_fetch_date = today
            holidays_str = ", ".join(d.isoformat() for d in sorted(self._holidays) if d >= today)
            logger.info("[SCHEDULE] Праздники загружены: %s", holidays_str or "нет")
        except Exception:
            logger.exception("[SCHEDULE] Не удалось загрузить расписание, работаем по будням")

    def is_open(self, now_msk: datetime) -> bool:
        """Проверить, открыт ли рынок: будни 07:00–23:50 МСК, без праздников."""
        if now_msk.weekday() >= 5:
            return False
        if now_msk.date() in self._holidays:
            return False
        t = now_msk.time()
        return _MARKET_OPEN <= t < _MARKET_CLOSE
