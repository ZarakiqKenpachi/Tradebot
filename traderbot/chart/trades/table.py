"""Trades table widget — QTableView with custom model and summary stats."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex, pyqtSignal, QPoint
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QTableView, QHeaderView, QWidget, QVBoxLayout, QLabel, QHBoxLayout, QMenu,
)

from traderbot.chart.trades.models import TradeDisplayRecord


COLUMNS = [
    ("#", "#"),
    ("direction", "Dir"),
    ("entry_price", "Entry"),
    ("exit_price", "Exit"),
    ("pnl", "P&L"),
    ("entry_time", "Time In"),
    ("exit_time", "Time Out"),
    ("exit_reason", "Result"),
    ("stop_price", "SL"),
    ("target_price", "TP"),
    ("candles_held", "Bars"),
    ("qty", "Qty"),
    ("ticker", "Ticker"),
]


class TradeTableModel(QAbstractTableModel):

    def __init__(self, parent=None):
        super().__init__(parent)
        self._trades: list[TradeDisplayRecord] = []

    def set_trades(self, trades: list[TradeDisplayRecord]) -> None:
        self.beginResetModel()
        self._trades = trades
        self.endResetModel()

    def get_trade(self, row: int) -> TradeDisplayRecord | None:
        if 0 <= row < len(self._trades):
            return self._trades[row]
        return None

    def rowCount(self, parent=QModelIndex()) -> int:
        return len(self._trades)

    def columnCount(self, parent=QModelIndex()) -> int:
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.ItemDataRole.DisplayRole):
        if role == Qt.ItemDataRole.DisplayRole and orientation == Qt.Orientation.Horizontal:
            return COLUMNS[section][1]
        return None

    def data(self, index: QModelIndex, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None

        trade = self._trades[index.row()]
        col_key = COLUMNS[index.column()][0]

        if col_key == "#":
            if role == Qt.ItemDataRole.DisplayRole:
                return str(index.row() + 1)
            if role == Qt.ItemDataRole.TextAlignmentRole:
                return Qt.AlignmentFlag.AlignCenter
            return None

        value = getattr(trade, col_key, "")

        if role == Qt.ItemDataRole.DisplayRole:
            if col_key in ("entry_price", "exit_price", "stop_price", "target_price"):
                return f"{value:.2f}" if value else "—"
            if col_key == "pnl":
                return f"{value:+.2f}" if value else "0.00"
            if col_key in ("entry_time", "exit_time"):
                s = str(value)
                # Show only date + time, no timezone
                if "T" in s:
                    return s.replace("T", " ")[:19]
                return s[:19] if len(s) > 19 else s
            if col_key == "exit_reason":
                # Friendly names
                r = str(value)
                if "take_profit" in r:
                    return "TP"
                if "stop_loss" in r:
                    return "SL"
                if "timeout" in r:
                    return "Timeout"
                if "end_of_data" in r:
                    return "EOD"
                return r
            return str(value)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col_key == "pnl":
                return QColor("#26a69a") if trade.pnl >= 0 else QColor("#ef5350")
            if col_key == "direction":
                return QColor("#26a69a") if trade.direction == "BUY" else QColor("#ef5350")
            if col_key == "exit_reason":
                r = str(value)
                if "take_profit" in r:
                    return QColor("#26a69a")
                if "stop_loss" in r:
                    return QColor("#ef5350")
                return QColor("#ff9800")

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col_key in ("entry_price", "exit_price", "stop_price", "target_price",
                           "pnl", "qty", "candles_held"):
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
            if col_key in ("direction", "exit_reason"):
                return Qt.AlignmentFlag.AlignCenter

        return None


class TradesPanel(QWidget):
    """Panel containing trades table with summary stats bar."""

    trade_selected = pyqtSignal(dict)
    trade_double_clicked = pyqtSignal(dict)
    scroll_to_trade = pyqtSignal(dict)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = TradeTableModel(self)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # ── Summary stats bar ────────────────────────────
        summary_widget = QWidget()
        summary_widget.setObjectName("summary-bar")
        summary_widget.setStyleSheet(
            "#summary-bar { background-color: #1a1e28; border-bottom: 1px solid #252830; }"
        )
        self._summary_layout = QHBoxLayout(summary_widget)
        self._summary_layout.setContentsMargins(8, 4, 8, 4)
        self._summary_layout.setSpacing(4)

        self._total_label = self._make_stat("Trades: 0")
        self._pnl_label = self._make_stat("P&L: —", accent=True)
        self._winrate_label = self._make_stat("Win: —")
        self._avg_label = self._make_stat("Avg: —")
        self._pf_label = self._make_stat("PF: —")
        self._max_dd_label = self._make_stat("MaxDD: —")

        for lbl in (self._total_label, self._pnl_label, self._winrate_label,
                    self._avg_label, self._pf_label, self._max_dd_label):
            self._summary_layout.addWidget(lbl)
        self._summary_layout.addStretch()

        layout.addWidget(summary_widget)

        # ── Table ────────────────────────────────────────
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)
        self._table.setShowGrid(False)

        # Context menu
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)

        # Column sizing
        header = self._table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setHighlightSections(False)

        # Row height
        self._table.verticalHeader().setDefaultSectionSize(26)

        # Signals
        self._table.clicked.connect(self._on_row_clicked)
        self._table.doubleClicked.connect(self._on_row_double_clicked)

        layout.addWidget(self._table)

    @staticmethod
    def _make_stat(text: str, accent: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setProperty("class", "summary-stat-accent" if accent else "summary-stat")
        lbl.setTextFormat(Qt.TextFormat.RichText)
        return lbl

    def set_trades(self, trades: list[TradeDisplayRecord]) -> None:
        self._model.set_trades(trades)
        self._update_summary(trades)

        for i in range(len(COLUMNS)):
            self._table.resizeColumnToContents(i)

    def _update_summary(self, trades: list[TradeDisplayRecord]) -> None:
        total = len(trades)
        self._total_label.setText(f"Trades: {total}")

        if total == 0:
            self._pnl_label.setText("P&L: —")
            self._winrate_label.setText("Win: —")
            self._avg_label.setText("Avg: —")
            self._pf_label.setText("PF: —")
            self._max_dd_label.setText("MaxDD: —")
            return

        total_pnl = sum(t.pnl for t in trades)
        wins = [t for t in trades if t.pnl > 0]
        losses = [t for t in trades if t.pnl <= 0]
        win_count = len(wins)
        winrate = (win_count / total) * 100
        avg_pnl = total_pnl / total

        # Profit factor
        gross_profit = sum(t.pnl for t in wins) if wins else 0
        gross_loss = abs(sum(t.pnl for t in losses)) if losses else 0
        pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # Max drawdown (sequential)
        peak = 0.0
        running = 0.0
        max_dd = 0.0
        for t in trades:
            running += t.pnl
            peak = max(peak, running)
            dd = peak - running
            max_dd = max(max_dd, dd)

        pnl_color = "#26a69a" if total_pnl >= 0 else "#ef5350"
        pnl_sign = "+" if total_pnl >= 0 else ""
        self._pnl_label.setText(
            f"P&L: <span style='color:{pnl_color}'>{pnl_sign}{total_pnl:.2f}</span>"
        )

        wr_color = "#26a69a" if winrate >= 50 else "#ef5350"
        self._winrate_label.setText(
            f"Win: <span style='color:{wr_color}'>{winrate:.0f}%</span>"
            f" <span style='color:#525669'>({win_count}/{total})</span>"
        )

        avg_color = "#26a69a" if avg_pnl >= 0 else "#ef5350"
        self._avg_label.setText(
            f"Avg: <span style='color:{avg_color}'>{avg_pnl:+.2f}</span>"
        )

        pf_color = "#26a69a" if pf >= 1.0 else "#ef5350"
        pf_text = f"{pf:.2f}" if pf != float("inf") else "max"
        self._pf_label.setText(
            f"PF: <span style='color:{pf_color}'>{pf_text}</span>"
        )

        self._max_dd_label.setText(
            f"MaxDD: <span style='color:#ef5350'>{max_dd:.2f}</span>"
        )

    def _on_row_clicked(self, index: QModelIndex) -> None:
        trade = self._model.get_trade(index.row())
        if trade:
            self.trade_selected.emit(trade.to_dict())

    def _on_row_double_clicked(self, index: QModelIndex) -> None:
        trade = self._model.get_trade(index.row())
        if trade:
            self.trade_double_clicked.emit(trade.to_dict())

    def _on_context_menu(self, pos: QPoint) -> None:
        index = self._table.indexAt(pos)
        if not index.isValid():
            return
        trade = self._model.get_trade(index.row())
        if not trade:
            return

        menu = QMenu(self)
        td = trade.to_dict()

        scroll_action = menu.addAction("Show on chart")
        scroll_action.triggered.connect(lambda: self.scroll_to_trade.emit(td))

        detail_action = menu.addAction("Trade details")
        detail_action.triggered.connect(lambda: self.trade_double_clicked.emit(td))

        menu.exec(self._table.viewport().mapToGlobal(pos))
