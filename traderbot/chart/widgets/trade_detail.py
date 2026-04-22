"""Trade detail popup — shown when clicking a trade marker or table row."""
from __future__ import annotations

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QGroupBox, QGridLayout,
    QPushButton, QHBoxLayout,
)


class TradeDetailDialog(QDialog):
    """Shows full details of a trade: entry/exit, reason, P&L."""

    def __init__(self, trade: dict, parent=None):
        super().__init__(parent)
        self._trade = trade
        self.setWindowTitle("Trade Details")
        self.setMinimumWidth(420)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        t = self._trade

        direction = t.get("direction", "?")
        ticker = t.get("ticker", "?")
        is_buy = direction == "BUY"
        dir_color = "#26a69a" if is_buy else "#ef5350"

        # Header
        header = QLabel(f"<h2 style='color:{dir_color}'>{direction} {ticker}</h2>")
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)

        # Entry group
        entry_group = QGroupBox("Entry")
        eg = QGridLayout(entry_group)
        eg.addWidget(QLabel("Price:"), 0, 0)
        eg.addWidget(QLabel(f"<b>{t.get('entry_price', '—')}</b>"), 0, 1)
        eg.addWidget(QLabel("Time:"), 1, 0)
        eg.addWidget(QLabel(str(t.get("entry_time", "—"))), 1, 1)
        eg.addWidget(QLabel("Reason:"), 2, 0)

        reason_label = QLabel(str(t.get("entry_reason", "—")))
        reason_label.setWordWrap(True)
        reason_label.setStyleSheet("color: #d1d4dc; padding: 4px;")
        eg.addWidget(reason_label, 2, 1)

        eg.addWidget(QLabel("Qty:"), 3, 0)
        eg.addWidget(QLabel(str(t.get("qty", "—"))), 3, 1)
        layout.addWidget(entry_group)

        # Exit group
        exit_group = QGroupBox("Exit")
        xg = QGridLayout(exit_group)
        xg.addWidget(QLabel("Price:"), 0, 0)
        xg.addWidget(QLabel(f"<b>{t.get('exit_price', '—')}</b>"), 0, 1)
        xg.addWidget(QLabel("Time:"), 1, 0)
        xg.addWidget(QLabel(str(t.get("exit_time", "—"))), 1, 1)
        xg.addWidget(QLabel("Reason:"), 2, 0)

        exit_reason_label = QLabel(str(t.get("exit_reason", "—")))
        exit_reason_label.setWordWrap(True)
        exit_reason_label.setStyleSheet("color: #d1d4dc; padding: 4px;")
        xg.addWidget(exit_reason_label, 2, 1)

        xg.addWidget(QLabel("Candles held:"), 3, 0)
        xg.addWidget(QLabel(str(t.get("candles_held", "—"))), 3, 1)
        layout.addWidget(exit_group)

        # Levels group
        levels_group = QGroupBox("Levels")
        lg = QGridLayout(levels_group)
        lg.addWidget(QLabel("Stop Loss:"), 0, 0)
        sl = t.get("stop_price", "—")
        lg.addWidget(QLabel(f"<span style='color:#ff9800'>{sl}</span>"), 0, 1)
        lg.addWidget(QLabel("Take Profit:"), 1, 0)
        tp = t.get("target_price", "—")
        lg.addWidget(QLabel(f"<span style='color:#2962ff'>{tp}</span>"), 1, 1)
        layout.addWidget(levels_group)

        # P&L
        pnl = t.get("pnl")
        commission = t.get("commission", 0)
        if pnl is not None:
            pnl_color = "#26a69a" if pnl >= 0 else "#ef5350"
            pnl_sign = "+" if pnl >= 0 else ""
            pnl_label = QLabel(
                f"<h3>P&L: <span style='color:{pnl_color}'>{pnl_sign}{pnl:.2f}</span>"
                f" <span style='color:#787b86; font-size:12px'>(commission: {commission:.4f})</span></h3>"
            )
            pnl_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(pnl_label)

        # Close button
        btn_layout = QHBoxLayout()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_layout.addStretch()
        btn_layout.addWidget(close_btn)
        btn_layout.addStretch()
        layout.addLayout(btn_layout)
