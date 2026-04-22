"""Trades table widget — QTableView with custom model."""
from __future__ import annotations

from PyQt6.QtCore import Qt, QAbstractTableModel, QModelIndex, pyqtSignal, QPoint
from PyQt6.QtGui import QColor, QAction
from PyQt6.QtWidgets import QTableView, QHeaderView, QWidget, QVBoxLayout, QLabel, QHBoxLayout, QMenu

from traderbot.chart.trades.models import TradeDisplayRecord


COLUMNS = [
    ("#", "#"),
    ("direction", "Dir"),
    ("entry_price", "Entry"),
    ("exit_price", "Exit"),
    ("pnl", "P&L"),
    ("entry_time", "Entry Time"),
    ("exit_time", "Exit Time"),
    ("exit_reason", "Reason"),
    ("stop_price", "SL"),
    ("target_price", "TP"),
    ("candles_held", "Bars"),
    ("qty", "Qty"),
    ("ticker", "Ticker"),
]


class TradeTableModel(QAbstractTableModel):
    """Model for the trades table."""

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
                if "T" in s:
                    return s.replace("T", " ")[:19]
                return s[:19] if len(s) > 19 else s
            return str(value)

        if role == Qt.ItemDataRole.ForegroundRole:
            if col_key == "pnl":
                return QColor("#26a69a") if trade.pnl >= 0 else QColor("#ef5350")
            if col_key == "direction":
                return QColor("#26a69a") if trade.direction == "BUY" else QColor("#ef5350")
            if col_key == "exit_reason":
                if "take_profit" in str(value):
                    return QColor("#26a69a")
                elif "stop_loss" in str(value):
                    return QColor("#ef5350")
                return QColor("#ff9800")

        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col_key in ("entry_price", "exit_price", "stop_price", "target_price", "pnl", "qty", "candles_held"):
                return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

        return None


class TradesPanel(QWidget):
    """Panel containing trades table with summary stats."""

    trade_selected = pyqtSignal(dict)  # trade dict when row clicked
    trade_double_clicked = pyqtSignal(dict)  # trade dict for detail popup
    scroll_to_trade = pyqtSignal(dict)  # right-click → show on chart

    def __init__(self, parent=None):
        super().__init__(parent)
        self._model = TradeTableModel(self)
        self._setup_ui()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Summary row
        self._summary_layout = QHBoxLayout()
        self._total_label = QLabel("Trades: 0")
        self._pnl_label = QLabel("Total P&L: 0.00")
        self._winrate_label = QLabel("Win Rate: —")
        self._avg_label = QLabel("Avg: —")
        for lbl in (self._total_label, self._pnl_label, self._winrate_label, self._avg_label):
            lbl.setStyleSheet("color: #787b86; font-size: 12px; padding: 2px 8px;")
            self._summary_layout.addWidget(lbl)
        self._summary_layout.addStretch()
        layout.addLayout(self._summary_layout)

        # Table
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setAlternatingRowColors(True)
        self._table.setSelectionBehavior(QTableView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QTableView.SelectionMode.SingleSelection)
        self._table.setSortingEnabled(True)
        self._table.verticalHeader().setVisible(False)

        # Context menu (right-click)
        self._table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._table.customContextMenuRequested.connect(self._on_context_menu)

        # Column sizing
        header = self._table.horizontalHeader()
        header.setStretchLastSection(True)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)

        # Connect signals
        self._table.clicked.connect(self._on_row_clicked)
        self._table.doubleClicked.connect(self._on_row_double_clicked)

        layout.addWidget(self._table)

    def set_trades(self, trades: list[TradeDisplayRecord]) -> None:
        """Update the table with new trade data."""
        self._model.set_trades(trades)
        self._update_summary(trades)

        # Auto-resize columns
        for i in range(len(COLUMNS)):
            self._table.resizeColumnToContents(i)

    def _update_summary(self, trades: list[TradeDisplayRecord]) -> None:
        total = len(trades)
        self._total_label.setText(f"Trades: {total}")

        if total == 0:
            self._pnl_label.setText("Total P&L: 0.00")
            self._winrate_label.setText("Win Rate: —")
            self._avg_label.setText("Avg: —")
            return

        total_pnl = sum(t.pnl for t in trades)
        wins = sum(1 for t in trades if t.pnl > 0)
        winrate = (wins / total) * 100
        avg_pnl = total_pnl / total

        pnl_color = "#26a69a" if total_pnl >= 0 else "#ef5350"
        self._pnl_label.setText(f"Total P&L: <span style='color:{pnl_color}'>{total_pnl:+.2f}</span>")
        self._pnl_label.setTextFormat(Qt.TextFormat.RichText)

        self._winrate_label.setText(f"Win Rate: {winrate:.1f}% ({wins}/{total})")

        avg_color = "#26a69a" if avg_pnl >= 0 else "#ef5350"
        self._avg_label.setText(f"Avg: <span style='color:{avg_color}'>{avg_pnl:+.2f}</span>")
        self._avg_label.setTextFormat(Qt.TextFormat.RichText)

    def _on_row_clicked(self, index: QModelIndex) -> None:
        trade = self._model.get_trade(index.row())
        if trade:
            self.trade_selected.emit(trade.to_dict())

    def _on_row_double_clicked(self, index: QModelIndex) -> None:
        trade = self._model.get_trade(index.row())
        if trade:
            self.trade_double_clicked.emit(trade.to_dict())

    def _on_context_menu(self, pos: QPoint) -> None:
        """Right-click context menu on trades table."""
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
