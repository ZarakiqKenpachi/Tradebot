"""Abstract candle data provider — swap implementations without touching UI."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

import pandas as pd


@dataclass
class SymbolInfo:
    """Symbol search result."""
    symbol: str
    exchange: str
    description: str
    symbol_type: str  # "stock", "crypto", "futures", etc.

    @property
    def full_symbol(self) -> str:
        return f"{self.exchange}:{self.symbol}"

    def __str__(self) -> str:
        return f"{self.exchange}:{self.symbol} — {self.description}"


class CandleProvider(Protocol):
    """Protocol for candle data sources.

    Implementations: TvDatafeedProvider (TradingView), future APIs.
    All providers must return DataFrames with columns:
        open, high, low, close, volume
    and a DatetimeIndex (UTC).
    """

    def get_candles(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        n_bars: int = 5000,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles.

        Args:
            symbol: Ticker symbol (e.g., "SBER", "BTCUSDT")
            exchange: Exchange name (e.g., "MOEX", "BINANCE")
            timeframe: One of: 1m, 5m, 15m, 30m, 1h, 2h, 4h, 1d, 1w
            n_bars: Number of bars to request

        Returns:
            DataFrame with OHLCV data, DatetimeIndex in UTC.
            Empty DataFrame if no data available.
        """
        ...

    def search_symbol(self, query: str, limit: int = 20) -> list[SymbolInfo]:
        """Search for symbols by name.

        Args:
            query: Search string (e.g., "SBER", "BTC")
            limit: Max results

        Returns:
            List of matching symbols.
        """
        ...

    def is_connected(self) -> bool:
        """Check if the provider is connected and operational."""
        ...
