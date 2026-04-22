"""Python ↔ JavaScript bridge via QWebChannel."""
from __future__ import annotations

import json
import logging

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)


class ChartBridge(QObject):
    """Bridge object exposed to JavaScript via QWebChannel.

    JS calls methods on this object; Python emits signals to JS.
    """

    # Signals from JS → Python
    chart_ready = pyqtSignal()
    marker_clicked = pyqtSignal(dict)       # trade marker data
    crosshair_moved = pyqtSignal(dict)      # OHLCV data at cursor

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = None

    def set_page(self, page) -> None:
        """Set the QWebEnginePage for calling JS."""
        self._page = page

    # ── JS → Python slots ────────────────────────────────

    @pyqtSlot()
    def chartReady(self):
        """Called by JS when chart is initialized."""
        logger.info("[BRIDGE] Chart ready")
        self.chart_ready.emit()

    @pyqtSlot(str)
    def onMarkerClicked(self, json_data: str):
        """Called by JS when user clicks a trade marker."""
        try:
            data = json.loads(json_data)
            self.marker_clicked.emit(data)
        except Exception:
            logger.exception("[BRIDGE] Failed to parse marker click data")

    @pyqtSlot(str)
    def onCrosshairMove(self, json_data: str):
        """Called by JS on crosshair move — OHLCV at cursor."""
        try:
            data = json.loads(json_data)
            self.crosshair_moved.emit(data)
        except Exception:
            pass  # High frequency, don't spam logs

    # ── Python → JS calls ────────────────────────────────

    def _run_js(self, script: str) -> None:
        """Execute JavaScript in the chart page."""
        if self._page:
            self._page.runJavaScript(script)

    def set_candles(self, candles_json: str) -> None:
        """Send candle data to chart."""
        self._run_js(f"setCandles('{self._escape(candles_json)}')")

    def update_candle(self, candle_json: str) -> None:
        """Update/append a single candle."""
        self._run_js(f"updateCandle('{self._escape(candle_json)}')")

    def append_candles(self, candles_json: str) -> None:
        """Append new candles."""
        self._run_js(f"appendCandles('{self._escape(candles_json)}')")

    def set_ema_data(self, fast_json: str, slow_json: str) -> None:
        """Send EMA overlay data."""
        self._run_js(f"setEmaData('{self._escape(fast_json)}', '{self._escape(slow_json)}')")

    def toggle_ema(self, visible: bool) -> None:
        self._run_js(f"toggleEma({'true' if visible else 'false'})")

    def set_trade_markers(self, markers_json: str) -> None:
        """Set trade entry/exit markers on the chart."""
        self._run_js(f"setTradeMarkers('{self._escape(markers_json)}')")

    def set_price_lines(self, lines_json: str) -> None:
        """Draw horizontal price lines (SL/TP levels)."""
        self._run_js(f"setPriceLines('{self._escape(lines_json)}')")

    def clear_price_lines(self) -> None:
        self._run_js("clearPriceLines()")

    def apply_theme(self, theme_json: str) -> None:
        """Apply color theme to chart."""
        self._run_js(f"applyTheme('{self._escape(theme_json)}')")

    def set_watermark(self, text: str) -> None:
        self._run_js(f"setWatermark('{self._escape(text)}')")

    def scroll_to_time(self, timestamp: int) -> None:
        self._run_js(f"scrollToTime({timestamp})")

    def fit_content(self) -> None:
        self._run_js("fitContent()")

    @staticmethod
    def _escape(s: str) -> str:
        """Escape string for JS single-quote context."""
        return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
