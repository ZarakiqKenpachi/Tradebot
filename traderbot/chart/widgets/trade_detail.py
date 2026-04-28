"""Trade detail popup — shown when clicking a trade marker or table row."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLabel, QGroupBox, QGridLayout,
    QPushButton, QHBoxLayout,
)


class TradeDetailDialog(QDialog):
    """Shows full details of a trade: entry/exit, reason, P&L."""

    scroll_to_trade = pyqtSignal(dict)

    def __init__(self, trade: dict, parent=None):
        super().__init__(parent)
        self._trade = trade
        self.setWindowTitle("Trade Details")
        self.setMinimumWidth(380)
        self.setMaximumWidth(500)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(8)
        t = self._trade

        direction = t.get("direction", "?")
        ticker = t.get("ticker", "?")
        is_buy = direction == "BUY"
        dir_color = "#26a69a" if is_buy else "#ef5350"

        # Header
        header = QLabel(
            f"<span style='color:{dir_color}; font-size:16px; font-weight:bold'>"
            f"{direction}</span>"
            f" <span style='color:#9598a1; font-size:16px'>{ticker}</span>"
        )
        header.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(header)

        # P&L (prominent, at top)
        pnl = t.get("pnl")
        if pnl is not None:
            pnl_color = "#26a69a" if pnl >= 0 else "#ef5350"
            pnl_sign = "+" if pnl >= 0 else ""
            # Calculate % move
            entry_p = t.get("entry_price")
            exit_p = t.get("exit_price")
            pct_str = ""
            if entry_p and exit_p and entry_p != 0:
                pct = (exit_p - entry_p) / entry_p * 100
                if t.get("direction") == "SELL":
                    pct = -pct
                pct_sign = "+" if pct >= 0 else ""
                pct_str = f"  <span style='color:{pnl_color}; font-size:14px'>({pct_sign}{pct:.2f}%)</span>"
            pnl_label = QLabel(
                f"<div style='text-align:center'>"
                f"<span style='color:{pnl_color}; font-size:20px; font-weight:bold'>"
                f"{pnl_sign}{pnl:.2f}</span>{pct_str}"
                f"</div>"
            )
            pnl_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            layout.addWidget(pnl_label)

        # Entry
        entry_group = QGroupBox("Entry")
        eg = QGridLayout(entry_group)
        eg.setSpacing(4)
        self._add_row(eg, 0, "Price", f"{t.get('entry_price', '—')}")
        self._add_row(eg, 1, "Time", self._fmt_time(t.get("entry_time", "—")))
        self._add_row(eg, 2, "Reason", str(t.get("entry_reason", "—")), wrap=True)
        self._add_row(eg, 3, "Qty", str(t.get("qty", "—")))
        layout.addWidget(entry_group)

        # Exit
        exit_group = QGroupBox("Exit")
        xg = QGridLayout(exit_group)
        xg.setSpacing(4)
        self._add_row(xg, 0, "Price", f"{t.get('exit_price', '—')}")
        self._add_row(xg, 1, "Time", self._fmt_time(t.get("exit_time", "—")))

        exit_reason = str(t.get("exit_reason", "—"))
        reason_color = "#26a69a" if "take_profit" in exit_reason else "#ef5350" if "stop_loss" in exit_reason else "#ff9800"
        self._add_row(xg, 2, "Result", exit_reason, color=reason_color)
        self._add_row(xg, 3, "Bars held", str(t.get("candles_held", "—")))
        layout.addWidget(exit_group)

        # Levels
        levels_group = QGroupBox("Levels")
        lg = QGridLayout(levels_group)
        lg.setSpacing(4)
        sl = t.get("stop_price")
        tp = t.get("target_price")
        entry_p = t.get("entry_price")
        sl_str = str(sl) if sl else "—"
        tp_str = str(tp) if tp else "—"
        if sl and entry_p and entry_p != 0:
            sl_pct = abs(sl - entry_p) / entry_p * 100
            sl_str += f"  (−{sl_pct:.2f}%)"
        if tp and entry_p and entry_p != 0:
            tp_pct = abs(tp - entry_p) / entry_p * 100
            tp_str += f"  (+{tp_pct:.2f}%)"
        self._add_row(lg, 0, "Stop Loss", sl_str, color="#ff9800")
        self._add_row(lg, 1, "Take Profit", tp_str, color="#2962ff")
        layout.addWidget(levels_group)

        # Buttons
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        goto_btn = QPushButton("Перейти")
        goto_btn.setProperty("class", "accent-btn")
        goto_btn.setFixedWidth(90)
        goto_btn.clicked.connect(self._on_goto)
        btn_layout.addWidget(goto_btn)

        close_btn = QPushButton("Закрыть")
        close_btn.setFixedWidth(90)
        close_btn.clicked.connect(self.accept)
        btn_layout.addWidget(close_btn)

        btn_layout.addStretch()
        layout.addLayout(btn_layout)

    def _on_goto(self) -> None:
        self.scroll_to_trade.emit(self._trade)
        self.accept()

    @staticmethod
    def _add_row(grid: QGridLayout, row: int, label: str, value: str,
                 color: str = "", wrap: bool = False) -> None:
        key_lbl = QLabel(label)
        key_lbl.setStyleSheet("color: #636674; font-size: 11px;")

        val_text = value
        if color:
            val_text = f"<span style='color:{color}'>{value}</span>"
        val_lbl = QLabel(val_text)
        val_lbl.setTextFormat(Qt.TextFormat.RichText)
        val_lbl.setStyleSheet("font-size: 12px;")
        if wrap:
            val_lbl.setWordWrap(True)

        grid.addWidget(key_lbl, row, 0)
        grid.addWidget(val_lbl, row, 1)

    @staticmethod
    def _fmt_time(t) -> str:
        s = str(t)
        if "T" in s:
            s = s.replace("T", " ")
        return s[:19]
