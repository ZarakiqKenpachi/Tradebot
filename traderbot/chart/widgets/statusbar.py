"""Status bar — connection, status text, OHLCV at cursor."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QStatusBar, QLabel, QWidget, QHBoxLayout


class ChartStatusBar(QStatusBar):
    """Bottom status bar: [Connected] | status text ... | O H L C V"""

    def __init__(self, parent=None):
        super().__init__(parent)

        # Connection indicator
        self._conn_label = QLabel("--")
        self._conn_label.setObjectName("disconnected_label")
        self.addWidget(self._conn_label)

        # Status text
        self._status_label = QLabel("Ready")
        self._status_label.setObjectName("status_label")
        self.addWidget(self._status_label, 1)

        # OHLCV display (right side, monospace)
        self._ohlcv_widget = QWidget()
        ohlcv_layout = QHBoxLayout(self._ohlcv_widget)
        ohlcv_layout.setContentsMargins(0, 0, 0, 0)
        ohlcv_layout.setSpacing(2)

        self._o_label = self._make_ohlcv_part("O", "—")
        self._h_label = self._make_ohlcv_part("H", "—")
        self._l_label = self._make_ohlcv_part("L", "—")
        self._c_label = self._make_ohlcv_part("C", "—")
        self._v_label = self._make_ohlcv_part("V", "—")
        self._change_label = QLabel("")
        self._change_label.setObjectName("ohlcv_label")

        for w in (self._o_label, self._h_label, self._l_label,
                  self._c_label, self._change_label, self._v_label):
            ohlcv_layout.addWidget(w)

        self.addPermanentWidget(self._ohlcv_widget)

    @staticmethod
    def _make_ohlcv_part(key: str, default: str) -> QLabel:
        lbl = QLabel(f"<span style='color:#525669'>{key}</span> {default}")
        lbl.setObjectName("ohlcv_label")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        return lbl

    def set_connected(self, connected: bool) -> None:
        if connected:
            self._conn_label.setText("TV")
            self._conn_label.setObjectName("connected_label")
        else:
            self._conn_label.setText("--")
            self._conn_label.setObjectName("disconnected_label")
        self._conn_label.style().unpolish(self._conn_label)
        self._conn_label.style().polish(self._conn_label)

    def set_status(self, text: str) -> None:
        self._status_label.setText(text)

    def set_ohlcv(self, data: dict) -> None:
        if not data:
            return

        o = data.get("open", 0)
        h = data.get("high", 0)
        l = data.get("low", 0)
        c = data.get("close", 0)
        v = data.get("volume", 0)

        change = c - o
        pct = (change / o * 100) if o != 0 else 0
        is_up = change >= 0
        c_color = "#26a69a" if is_up else "#ef5350"
        dim = "#525669"

        self._o_label.setText(f"<span style='color:{dim}'>O</span> {o:.2f}")
        self._h_label.setText(f"<span style='color:{dim}'>H</span> {h:.2f}")
        self._l_label.setText(f"<span style='color:{dim}'>L</span> {l:.2f}")
        self._c_label.setText(
            f"<span style='color:{dim}'>C</span> "
            f"<span style='color:{c_color}'>{c:.2f}</span>"
        )

        sign = "+" if is_up else ""
        self._change_label.setText(
            f"<span style='color:{c_color}'>{sign}{change:.2f} ({sign}{pct:.2f}%)</span>"
        )

        self._v_label.setText(
            f"<span style='color:{dim}'>V</span> {self._fmt_vol(v)}"
        )

    @staticmethod
    def _fmt_vol(v: float) -> str:
        if v >= 1_000_000:
            return f"{v / 1_000_000:.1f}M"
        if v >= 1_000:
            return f"{v / 1_000:.1f}K"
        return f"{v:.0f}"
