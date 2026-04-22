"""Status bar with connection status, bar count, OHLCV at cursor."""
from __future__ import annotations

from PyQt6.QtWidgets import QStatusBar, QLabel


class ChartStatusBar(QStatusBar):
    """Bottom status bar showing connection state, data info, OHLCV."""

    def __init__(self, parent=None):
        super().__init__(parent)

        # Connection indicator
        self._conn_label = QLabel("Disconnected")
        self._conn_label.setObjectName("disconnected_label")
        self.addWidget(self._conn_label)

        # Separator
        self.addWidget(QLabel(" | "))

        # Status text
        self._status_label = QLabel("Ready")
        self._status_label.setObjectName("status_label")
        self.addWidget(self._status_label, 1)

        # OHLCV display (right side)
        self._ohlcv_label = QLabel("")
        self._ohlcv_label.setObjectName("status_label")
        self.addPermanentWidget(self._ohlcv_label)

    def set_connected(self, connected: bool) -> None:
        if connected:
            self._conn_label.setText("TV Connected")
            self._conn_label.setObjectName("connected_label")
        else:
            self._conn_label.setText("Disconnected")
            self._conn_label.setObjectName("disconnected_label")
        # Force style refresh
        self._conn_label.style().unpolish(self._conn_label)
        self._conn_label.style().polish(self._conn_label)

    def set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def set_ohlcv(self, data: dict) -> None:
        """Update OHLCV display from crosshair data."""
        if not data:
            self._ohlcv_label.setText("")
            return

        o = data.get("open", 0)
        h = data.get("high", 0)
        l = data.get("low", 0)
        c = data.get("close", 0)
        v = data.get("volume", 0)

        change = c - o
        color = "#26a69a" if change >= 0 else "#ef5350"

        self._ohlcv_label.setText(
            f"O: {o:.2f}  H: {h:.2f}  L: {l:.2f}  "
            f"C: <span style='color:{color}'>{c:.2f}</span>  "
            f"V: {self._format_volume(v)}"
        )

    @staticmethod
    def _format_volume(v: float) -> str:
        if v >= 1_000_000:
            return f"{v/1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v/1_000:.1f}K"
        return f"{v:.0f}"
