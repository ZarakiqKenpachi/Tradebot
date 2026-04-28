"""T-Bank candle provider for chart display — real MOEX exchange data."""
from __future__ import annotations

import logging
import math

import pandas as pd
from t_tech.invest import CandleInterval, InstrumentStatus

from traderbot.broker.tbank import TBankBroker
from traderbot.chart.data.provider import SymbolInfo
from traderbot.data.feed import DataFeed, filter_moex_hours

logger = logging.getLogger(__name__)

# Direct TF → CandleInterval mapping (no 1m resampling needed)
_INTERVAL_MAP: dict[str, CandleInterval] = {
    "1m": CandleInterval.CANDLE_INTERVAL_1_MIN,
    "3m": CandleInterval.CANDLE_INTERVAL_3_MIN,
    "5m": CandleInterval.CANDLE_INTERVAL_5_MIN,
    "15m": CandleInterval.CANDLE_INTERVAL_15_MIN,
    "30m": CandleInterval.CANDLE_INTERVAL_30_MIN,
    "1h": CandleInterval.CANDLE_INTERVAL_HOUR,
    "2h": CandleInterval.CANDLE_INTERVAL_2_HOUR,
    "4h": CandleInterval.CANDLE_INTERVAL_4_HOUR,
    "1d": CandleInterval.CANDLE_INTERVAL_DAY,
    "1w": CandleInterval.CANDLE_INTERVAL_WEEK,
}

# Approximate bars-per-day for converting n_bars → days
_BARS_PER_DAY = {
    "1m": 1020, "3m": 340, "5m": 204, "15m": 68, "30m": 34,
    "1h": 17, "2h": 9, "4h": 5, "1d": 1, "1w": 0.2,
}


class TBankCandleProvider:
    """CandleProvider implementation using T-Bank Invest API.

    Requests candles in the native timeframe directly from the API.
    Loads MOEX shares list for symbol search.
    """

    def __init__(self, feed: DataFeed, ticker_figi_map: dict[str, str]):
        self._feed = feed
        self._broker: TBankBroker = feed.broker
        self._figi_map = dict(ticker_figi_map)  # symbol → FIGI (mutable copy)
        self._all_shares: list[SymbolInfo] = []  # loaded lazily

    def get_candles(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        n_bars: int = 5000,
    ) -> pd.DataFrame:
        figi = self._figi_map.get(symbol)
        if not figi:
            logger.warning("[TBANK_PROVIDER] No FIGI for %s, cannot load", symbol)
            return pd.DataFrame()

        interval = _INTERVAL_MAP.get(timeframe)
        if not interval:
            logger.warning("[TBANK_PROVIDER] Unsupported timeframe %s", timeframe)
            return pd.DataFrame()

        # Convert n_bars to days
        bpd = _BARS_PER_DAY.get(timeframe, 34)
        days = max(3, math.ceil(n_bars / bpd) + 2) if bpd > 0 else 365

        try:
            df = self._broker.get_candles_history(figi, interval, days)
        except Exception:
            logger.exception("[TBANK_PROVIDER] Failed to load %s %s", symbol, timeframe)
            return pd.DataFrame()

        if df.empty:
            logger.warning("[TBANK_PROVIDER] No data for %s %s", symbol, timeframe)
            return pd.DataFrame()

        # Filter MOEX hours (except daily/weekly)
        if timeframe not in ("1d", "1w"):
            df = filter_moex_hours(df)

        # Trim to requested n_bars
        if len(df) > n_bars:
            df = df.iloc[-n_bars:]

        logger.info("[TBANK_PROVIDER] %s %s: %d bars", symbol, timeframe, len(df))
        return df

    def search_symbol(self, query: str, limit: int = 20) -> list[SymbolInfo]:
        """Search MOEX shares from T-Bank API."""
        self._ensure_shares_loaded()
        q = query.strip().upper()
        if not q:
            return []
        results = []
        for info in self._all_shares:
            if q in info.symbol.upper() or q in info.description.upper():
                results.append(info)
                if len(results) >= limit:
                    break
        return results

    def get_all_symbols(self) -> list[SymbolInfo]:
        """Return all available MOEX shares."""
        self._ensure_shares_loaded()
        return list(self._all_shares)

    def register_symbol(self, symbol: str, figi: str) -> None:
        """Add a symbol→FIGI mapping (e.g. when user selects a new ticker)."""
        self._figi_map[symbol] = figi

    def is_connected(self) -> bool:
        return True

    def _ensure_shares_loaded(self) -> None:
        """Load MOEX shares list from T-Bank API (once)."""
        if self._all_shares:
            return
        try:
            with self._broker._client() as client:
                resp = client.instruments.shares(
                    instrument_status=InstrumentStatus.INSTRUMENT_STATUS_BASE,
                )
            for s in resp.instruments:
                if "moex" not in s.exchange.lower():
                    continue
                self._all_shares.append(SymbolInfo(
                    symbol=s.ticker,
                    exchange="MOEX",
                    description=s.name,
                    symbol_type="stock",
                ))
                # Auto-register FIGI for any MOEX share
                if s.ticker not in self._figi_map:
                    self._figi_map[s.ticker] = s.figi
            self._all_shares.sort(key=lambda x: x.symbol)
            logger.info("[TBANK_PROVIDER] Loaded %d MOEX shares", len(self._all_shares))
        except Exception:
            logger.exception("[TBANK_PROVIDER] Failed to load shares list")
