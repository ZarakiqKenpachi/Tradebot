"""Symbol search dialog with results list."""
from __future__ import annotations

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog, QVBoxLayout, QLineEdit, QListWidget, QListWidgetItem,
    QLabel, QDialogButtonBox, QHBoxLayout,
)

from traderbot.chart.data.provider import SymbolInfo
from traderbot.chart.data.symbol_search import SymbolSearchService


class SymbolSearchDialog(QDialog):
    """Modal dialog for searching and selecting a symbol."""

    symbol_selected = pyqtSignal(SymbolInfo)

    def __init__(self, search_service: SymbolSearchService, parent=None):
        super().__init__(parent)
        self._search = search_service
        self._results: list[SymbolInfo] = []

        self.setWindowTitle("Symbol Search")
        self.setMinimumSize(500, 400)
        self._setup_ui()
        self._show_recent()

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)

        # Search input
        search_layout = QHBoxLayout()
        self._input = QLineEdit()
        self._input.setPlaceholderText("Enter symbol (SBER, GAZP, BTCUSDT...)")
        self._input.textChanged.connect(self._on_search)
        self._input.returnPressed.connect(self._on_accept)
        search_layout.addWidget(self._input)
        layout.addLayout(search_layout)

        # Hint
        hint = QLabel("Type to search. Double-click or Enter to select.")
        hint.setStyleSheet("color: #787b86; font-size: 11px;")
        layout.addWidget(hint)

        # Results list
        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.itemDoubleClicked.connect(self._on_item_double_clicked)
        layout.addWidget(self._list)

        # Buttons
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _show_recent(self) -> None:
        """Show recently used symbols."""
        recent = self._search.get_recent(15)
        self._results = recent
        self._list.clear()
        if recent:
            self._list.addItem(QListWidgetItem("── Recent ──"))
            for info in recent:
                item = QListWidgetItem(f"{info.exchange}:{info.symbol}  —  {info.description}")
                item.setData(Qt.ItemDataRole.UserRole, info)
                self._list.addItem(item)

    def _on_search(self, text: str) -> None:
        if len(text) < 1:
            self._show_recent()
            return

        results = self._search.search(text.strip(), limit=20)
        self._results = results
        self._list.clear()

        for info in results:
            type_tag = f"[{info.symbol_type}]" if info.symbol_type else ""
            item = QListWidgetItem(
                f"{info.exchange}:{info.symbol}  —  {info.description} {type_tag}"
            )
            item.setData(Qt.ItemDataRole.UserRole, info)
            self._list.addItem(item)

        if not results:
            self._list.addItem(QListWidgetItem("No results found"))

    def _on_item_double_clicked(self, item: QListWidgetItem) -> None:
        info = item.data(Qt.ItemDataRole.UserRole)
        if info:
            self._search.add_to_history(info)
            self.symbol_selected.emit(info)
            self.accept()

    def _on_accept(self) -> None:
        current = self._list.currentItem()
        if current:
            info = current.data(Qt.ItemDataRole.UserRole)
            if info:
                self._search.add_to_history(info)
                self.symbol_selected.emit(info)
                self.accept()
                return

        # If no item selected but text entered, try first result
        if self._results:
            info = self._results[0]
            self._search.add_to_history(info)
            self.symbol_selected.emit(info)
            self.accept()

    def get_selected(self) -> SymbolInfo | None:
        current = self._list.currentItem()
        if current:
            return current.data(Qt.ItemDataRole.UserRole)
        return self._results[0] if self._results else None
