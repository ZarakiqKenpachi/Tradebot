"""Python <-> JavaScript bridge via QWebChannel."""
from __future__ import annotations

import json
import logging

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

logger = logging.getLogger(__name__)


class ChartBridge(QObject):
    """Bridge object exposed to JavaScript via QWebChannel."""

    # Signals from JS -> Python
    chart_ready = pyqtSignal()
    marker_clicked = pyqtSignal(dict)
    crosshair_moved = pyqtSignal(dict)
    tool_deactivated = pyqtSignal()  # JS cancelled active tool (Escape/RMB)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._page = None

    def set_page(self, page) -> None:
        self._page = page

    # ── JS -> Python slots ───────────────────────────────

    @pyqtSlot()
    def chartReady(self):
        logger.info("[BRIDGE] Chart ready")
        self.chart_ready.emit()

    @pyqtSlot(str)
    def onMarkerClicked(self, json_data: str):
        try:
            self.marker_clicked.emit(json.loads(json_data))
        except Exception:
            logger.exception("[BRIDGE] Failed to parse marker click")

    @pyqtSlot(str)
    def onCrosshairMove(self, json_data: str):
        try:
            self.crosshair_moved.emit(json.loads(json_data))
        except Exception:
            pass

    @pyqtSlot(str)
    def onLineDrawn(self, json_data: str):
        pass

    @pyqtSlot()
    def onToolDeactivated(self):
        """JS cancelled active tool via Escape or right-click."""
        self.tool_deactivated.emit()

    # ── Python -> JS calls ───────────────────────────────

    def _run_js(self, script: str) -> None:
        if self._page:
            self._page.runJavaScript(script)

    # Data
    def set_candles(self, j: str) -> None:
        self._run_js(f"setCandles('{self._escape(j)}')")

    def update_candle(self, j: str) -> None:
        self._run_js(f"updateCandle('{self._escape(j)}')")

    def append_candles(self, j: str) -> None:
        self._run_js(f"appendCandles('{self._escape(j)}')")

    # Overlays
    def set_ema_data(self, fast: str, slow: str) -> None:
        self._run_js(f"setEmaData('{self._escape(fast)}', '{self._escape(slow)}')")

    def toggle_ema(self, visible: bool) -> None:
        self._run_js(f"toggleEma({'true' if visible else 'false'})")

    def set_rsi_data(self, j: str) -> None:
        self._run_js(f"setRsiData('{self._escape(j)}')")

    def remove_rsi(self) -> None:
        self._run_js("removeRsi()")

    def set_macd_data(self, line: str, signal: str, hist: str) -> None:
        self._run_js(f"setMacdData('{self._escape(line)}','{self._escape(signal)}','{self._escape(hist)}')")

    def remove_macd(self) -> None:
        self._run_js("removeMacd()")

    def set_bollinger_data(self, upper: str, middle: str, lower: str) -> None:
        self._run_js(f"setBollingerData('{self._escape(upper)}','{self._escape(middle)}','{self._escape(lower)}')")

    def remove_bollinger(self) -> None:
        self._run_js("removeBollinger()")

    # Markers & price lines
    def set_trade_markers(self, j: str) -> None:
        self._run_js(f"setTradeMarkers('{self._escape(j)}')")

    def set_price_lines(self, j: str) -> None:
        self._run_js(f"setPriceLines('{self._escape(j)}')")

    def clear_price_lines(self) -> None:
        self._run_js("clearPriceLines()")

    # Drawing tools
    def set_active_tool(self, tool: str) -> None:
        """Set active drawing tool: trendline, hray, fib, rect, measure, or empty to deactivate."""
        if tool:
            self._run_js(f"setActiveTool('{tool}')")
        else:
            self._run_js("setActiveTool(null)")

    def undo_drawing(self) -> None:
        self._run_js("undoDrawing()")

    def clear_all_drawings(self) -> None:
        self._run_js("clearAllDrawings()")

    def add_horizontal_line(self, price: float, color: str = "#787b86", title: str = "") -> None:
        self._run_js(f"addHorizontalLine({price}, '{color}', '{self._escape(title)}')")

    def remove_all_user_lines(self) -> None:
        self._run_js("removeAllUserLines()")

    def remove_last_user_line(self) -> None:
        self._run_js("removeLastUserLine()")

    # Navigation
    def set_crosshair_mode(self, mode: str) -> None:
        self._run_js(f"setCrosshairMode('{mode}')")

    def set_price_scale_mode(self, mode: str) -> None:
        self._run_js(f"setPriceScaleMode('{mode}')")

    def scroll_to_time(self, timestamp: int) -> None:
        self._run_js(f"scrollToTime({timestamp})")

    def fit_content(self) -> None:
        self._run_js("fitContent()")

    def take_screenshot(self) -> None:
        self._run_js("takeScreenshot()")

    # Theme
    def apply_theme(self, j: str) -> None:
        self._run_js(f"applyTheme('{self._escape(j)}')")

    def set_watermark(self, text: str) -> None:
        self._run_js(f"setWatermark('{self._escape(text)}')")

    # Legacy compat
    def toggle_drawing_mode(self, enabled: bool) -> None:
        self.set_active_tool("hray" if enabled else "")

    @staticmethod
    def _escape(s: str) -> str:
        return s.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
