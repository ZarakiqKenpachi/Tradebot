"""Chart widget — QWebEngineView hosting Lightweight Charts."""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pandas as pd
from PyQt6.QtCore import QUrl, QTimer, pyqtSignal
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QWidget, QVBoxLayout

from traderbot.chart.chart.bridge import ChartBridge
from traderbot.chart.chart.markers import trades_to_markers, trade_to_price_lines
from traderbot.chart.config import ChartTheme, REFRESH_INTERVALS, BARS_PER_TIMEFRAME
from traderbot.chart.data.provider import CandleProvider
from traderbot.chart.data.cache import CandleCache

logger = logging.getLogger(__name__)

JS_DIR = Path(__file__).parent / "js"


class ChartWidget(QWidget):
    """Main chart widget embedding Lightweight Charts via WebView."""

    # Signals
    candles_loaded = pyqtSignal(int)          # number of bars loaded
    status_changed = pyqtSignal(str)          # status text
    trade_marker_clicked = pyqtSignal(dict)   # marker data
    crosshair_data = pyqtSignal(dict)         # OHLCV at cursor

    def __init__(
        self,
        provider: CandleProvider,
        cache: CandleCache,
        theme: ChartTheme,
        parent=None,
    ):
        super().__init__(parent)
        self._provider = provider
        self._cache = cache
        self._theme = theme
        self._current_symbol = ""
        self._current_exchange = ""
        self._current_timeframe = "1h"
        self._candle_df: pd.DataFrame = pd.DataFrame()
        self._chart_ready = False
        self._auto_refresh = True

        # Layout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # WebView
        self._web = QWebEngineView()
        layout.addWidget(self._web)

        # Bridge
        self._bridge = ChartBridge(self)
        self._channel = QWebChannel()
        self._channel.registerObject("bridge", self._bridge)
        self._web.page().setWebChannel(self._channel)
        self._bridge.set_page(self._web.page())

        # Connect bridge signals
        self._bridge.chart_ready.connect(self._on_chart_ready)
        self._bridge.marker_clicked.connect(self.trade_marker_clicked.emit)
        self._bridge.crosshair_moved.connect(self.crosshair_data.emit)

        # Auto-refresh timer
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh_candles)

        # Load HTML
        html_path = JS_DIR / "chart.html"
        self._web.setUrl(QUrl.fromLocalFile(str(html_path)))

    def _on_chart_ready(self) -> None:
        """Chart JS initialized, apply theme and load data if pending."""
        self._chart_ready = True
        self._apply_theme()
        if self._current_symbol:
            self.load_symbol(self._current_symbol, self._current_exchange, self._current_timeframe)

    # ── Public API ───────────────────────────────────────

    def load_symbol(self, symbol: str, exchange: str, timeframe: str) -> None:
        """Load candles for a symbol/exchange/timeframe."""
        self._current_symbol = symbol
        self._current_exchange = exchange
        self._current_timeframe = timeframe

        if not self._chart_ready:
            return

        self.status_changed.emit(f"Loading {exchange}:{symbol} {timeframe}...")
        self._bridge.set_watermark(f"{exchange}:{symbol}")

        # Try cache first
        n_bars = BARS_PER_TIMEFRAME.get(timeframe, 5000)
        refresh_sec = REFRESH_INTERVALS.get(timeframe, 60)

        cached = self._cache.get(symbol, exchange, timeframe, max_age_seconds=refresh_sec)
        if cached is not None and not cached.empty:
            self._candle_df = cached
            self._send_candles_to_chart()
            self.status_changed.emit(f"{exchange}:{symbol} {timeframe} — {len(cached)} bars (cached)")
        else:
            self._fetch_and_display(symbol, exchange, timeframe, n_bars)

        # Restart auto-refresh timer
        self._setup_refresh_timer(timeframe)

    def change_timeframe(self, timeframe: str) -> None:
        """Switch timeframe for current symbol."""
        if self._current_symbol:
            self.load_symbol(self._current_symbol, self._current_exchange, timeframe)

    def set_auto_refresh(self, enabled: bool) -> None:
        self._auto_refresh = enabled
        if not enabled:
            self._refresh_timer.stop()
        elif self._current_symbol:
            self._setup_refresh_timer(self._current_timeframe)

    def set_theme(self, theme: ChartTheme) -> None:
        self._theme = theme
        if self._chart_ready:
            self._apply_theme()

    def set_trade_markers(self, trades: list[dict]) -> None:
        """Display trade entry/exit markers on the chart."""
        if not self._chart_ready:
            return
        markers_json = trades_to_markers(trades)
        self._bridge.set_trade_markers(markers_json)

    def highlight_trade(self, trade: dict) -> None:
        """Highlight a specific trade with price lines and scroll to it."""
        if not self._chart_ready:
            return
        lines_json = trade_to_price_lines(trade)
        self._bridge.set_price_lines(lines_json)

        # Scroll to entry time
        entry_time = trade.get("entry_time")
        if entry_time:
            from traderbot.chart.chart.markers import _to_timestamp
            ts = _to_timestamp(entry_time)
            if ts:
                self._bridge.scroll_to_time(ts)

    def clear_trade_highlight(self) -> None:
        if self._chart_ready:
            self._bridge.clear_price_lines()

    def set_ema(self, fast_period: int = 20, slow_period: int = 50, visible: bool = True) -> None:
        """Calculate and display EMA overlays."""
        if self._candle_df.empty or not self._chart_ready:
            return

        close = self._candle_df["close"]
        ema_fast = close.ewm(span=fast_period, adjust=False).mean()
        ema_slow = close.ewm(span=slow_period, adjust=False).mean()

        fast_data = self._series_to_json(ema_fast)
        slow_data = self._series_to_json(ema_slow)

        self._bridge.set_ema_data(fast_data, slow_data)
        self._bridge.toggle_ema(visible)

    def toggle_ema(self, visible: bool) -> None:
        if self._chart_ready:
            self._bridge.toggle_ema(visible)

    def fit_content(self) -> None:
        if self._chart_ready:
            self._bridge.fit_content()

    def set_crosshair_mode(self, mode: str) -> None:
        if self._chart_ready:
            self._bridge.set_crosshair_mode(mode)

    def toggle_drawing_mode(self, enabled: bool) -> None:
        if self._chart_ready:
            self._bridge.toggle_drawing_mode(enabled)

    def clear_user_lines(self) -> None:
        if self._chart_ready:
            self._bridge.remove_all_user_lines()

    def get_candle_df(self) -> pd.DataFrame:
        """Return current candle DataFrame for strategy testing."""
        return self._candle_df.copy()

    @property
    def current_symbol(self) -> str:
        return self._current_symbol

    @property
    def current_exchange(self) -> str:
        return self._current_exchange

    @property
    def current_timeframe(self) -> str:
        return self._current_timeframe

    # ── Private ──────────────────────────────────────────

    def _fetch_and_display(self, symbol: str, exchange: str, timeframe: str, n_bars: int) -> None:
        """Fetch from provider, cache, and display."""
        try:
            df = self._provider.get_candles(symbol, exchange, timeframe, n_bars)
        except Exception:
            logger.exception("[CHART] Failed to fetch candles")
            self.status_changed.emit(f"Error loading {exchange}:{symbol}")
            return

        if df.empty:
            self.status_changed.emit(f"No data for {exchange}:{symbol} {timeframe}")
            return

        self._candle_df = df
        self._cache.put(symbol, exchange, timeframe, df)
        self._send_candles_to_chart()
        self.candles_loaded.emit(len(df))
        self.status_changed.emit(
            f"{exchange}:{symbol} {timeframe} — {len(df)} bars | "
            f"Last: {df.index[-1].strftime('%Y-%m-%d %H:%M')}"
        )

    def _send_candles_to_chart(self) -> None:
        """Convert DataFrame to JSON and send to JS chart."""
        if self._candle_df.empty:
            return

        data = []
        for ts, row in self._candle_df.iterrows():
            data.append({
                "time": int(ts.timestamp()),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })

        self._bridge.set_candles(json.dumps(data))

    def _refresh_candles(self) -> None:
        """Auto-refresh: fetch latest candles and update chart."""
        if not self._current_symbol or not self._chart_ready:
            return

        try:
            # Fetch a small batch for update
            df = self._provider.get_candles(
                self._current_symbol, self._current_exchange,
                self._current_timeframe, n_bars=50,
            )
        except Exception:
            logger.debug("[CHART] Refresh failed")
            return

        if df.empty:
            return

        # Find new candles not in current data
        if not self._candle_df.empty:
            last_time = self._candle_df.index[-1]
            new_candles = df[df.index >= last_time]
        else:
            new_candles = df

        if new_candles.empty:
            return

        # Update internal DataFrame
        self._candle_df = pd.concat([self._candle_df, new_candles[~new_candles.index.isin(self._candle_df.index)]])
        self._candle_df = self._candle_df.sort_index()

        # Update the last candle (may have changed) and append new ones
        update_data = []
        for ts, row in new_candles.iterrows():
            update_data.append({
                "time": int(ts.timestamp()),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })

        if update_data:
            self._bridge.append_candles(json.dumps(update_data))

        # Update cache
        self._cache.put(
            self._current_symbol, self._current_exchange,
            self._current_timeframe, self._candle_df,
        )

    def _setup_refresh_timer(self, timeframe: str) -> None:
        self._refresh_timer.stop()
        if self._auto_refresh:
            interval_sec = REFRESH_INTERVALS.get(timeframe, 60)
            self._refresh_timer.start(interval_sec * 1000)

    def _apply_theme(self) -> None:
        theme_data = {
            "background": self._theme.background,
            "text": self._theme.text,
            "grid": self._theme.grid,
            "border": self._theme.border,
            "crosshair": self._theme.crosshair,
            "up_color": self._theme.up_color,
            "down_color": self._theme.down_color,
            "volume_up": self._theme.volume_up,
            "volume_down": self._theme.volume_down,
            "ema_fast_color": self._theme.ema_fast_color,
            "ema_slow_color": self._theme.ema_slow_color,
        }
        self._bridge.apply_theme(json.dumps(theme_data))

    @staticmethod
    def _series_to_json(series: pd.Series) -> str:
        """Convert pandas Series to JSON array of {time, value}."""
        data = []
        for ts, val in series.items():
            if pd.notna(val):
                data.append({"time": int(ts.timestamp()), "value": round(float(val), 4)})
        return json.dumps(data)
