"""Main toolbar — symbol search, timeframe buttons, indicators toggle."""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal, Qt
from PyQt6.QtWidgets import (
    QToolBar, QPushButton, QLineEdit, QButtonGroup, QWidget, QHBoxLayout,
    QCheckBox, QComboBox, QLabel,
)

from traderbot.chart.config import TIMEFRAMES


class MainToolbar(QToolBar):
    """Top toolbar with symbol search, timeframe selector, and controls."""

    # Signals
    symbol_search_requested = pyqtSignal(str)   # search query
    timeframe_changed = pyqtSignal(str)          # "1h", "4h", etc.
    ema_toggled = pyqtSignal(bool)               # show/hide EMA
    auto_refresh_toggled = pyqtSignal(bool)      # auto-refresh on/off
    theme_toggle_requested = pyqtSignal()        # switch dark/light
    strategy_run_requested = pyqtSignal(str)     # strategy name
    fit_requested = pyqtSignal()                 # fit chart to content

    def __init__(self, parent=None):
        super().__init__("Main Toolbar", parent)
        self.setMovable(False)
        self.setFloatable(False)

        # ── Symbol search ────────────────────────────────
        self._symbol_input = QLineEdit()
        self._symbol_input.setPlaceholderText("Symbol (SBER, BTCUSDT...)")
        self._symbol_input.setFixedWidth(200)
        self._symbol_input.returnPressed.connect(
            lambda: self.symbol_search_requested.emit(self._symbol_input.text())
        )
        self.addWidget(self._symbol_input)

        search_btn = QPushButton("Search")
        search_btn.clicked.connect(
            lambda: self.symbol_search_requested.emit(self._symbol_input.text())
        )
        self.addWidget(search_btn)
        self.addSeparator()

        # ── Timeframe buttons ────────────────────────────
        self._tf_group = QButtonGroup(self)
        self._tf_group.setExclusive(True)
        self._tf_buttons: dict[str, QPushButton] = {}

        for tf_key, tf_label in TIMEFRAMES:
            btn = QPushButton(tf_label)
            btn.setCheckable(True)
            btn.setFixedWidth(55)
            btn.setProperty("timeframe", tf_key)
            self._tf_group.addButton(btn)
            self._tf_buttons[tf_key] = btn
            self.addWidget(btn)

        self._tf_group.buttonClicked.connect(self._on_tf_clicked)

        # Set default
        if "1h" in self._tf_buttons:
            self._tf_buttons["1h"].setChecked(True)

        self.addSeparator()

        # ── Indicators ───────────────────────────────────
        self._ema_check = QCheckBox("EMA 20/50")
        self._ema_check.setChecked(True)
        self._ema_check.toggled.connect(self.ema_toggled.emit)
        self.addWidget(self._ema_check)

        self.addSeparator()

        # ── Strategy selector ────────────────────────────
        self.addWidget(QLabel(" Strategy: "))
        self._strategy_combo = QComboBox()
        self._strategy_combo.setFixedWidth(180)
        self._strategy_combo.addItem("(none)", "")
        self.addWidget(self._strategy_combo)

        self.addWidget(QLabel(" Days: "))
        self._days_combo = QComboBox()
        self._days_combo.setFixedWidth(70)
        for d in [3, 7, 14, 30, 60, 90, 180, 365]:
            self._days_combo.addItem(f"{d}d", d)
        self._days_combo.setCurrentIndex(2)  # default 14 days
        self.addWidget(self._days_combo)

        run_btn = QPushButton("Run")
        run_btn.setFixedWidth(50)
        run_btn.clicked.connect(self._on_strategy_run)
        self.addWidget(run_btn)

        self.addSeparator()

        # ── Controls ─────────────────────────────────────
        # Spacer
        spacer = QWidget()
        spacer.setFixedWidth(20)
        self.addWidget(spacer)

        self._refresh_check = QCheckBox("Auto-refresh")
        self._refresh_check.setChecked(True)
        self._refresh_check.toggled.connect(self.auto_refresh_toggled.emit)
        self.addWidget(self._refresh_check)

        fit_btn = QPushButton("Fit")
        fit_btn.setFixedWidth(40)
        fit_btn.setToolTip("Fit chart to content")
        fit_btn.clicked.connect(self.fit_requested.emit)
        self.addWidget(fit_btn)

        theme_btn = QPushButton("Theme")
        theme_btn.setFixedWidth(55)
        theme_btn.setToolTip("Toggle dark/light theme")
        theme_btn.clicked.connect(self.theme_toggle_requested.emit)
        self.addWidget(theme_btn)

    # ── Public API ───────────────────────────────────────

    def set_symbol_text(self, text: str) -> None:
        self._symbol_input.setText(text)

    def set_timeframe(self, tf: str) -> None:
        btn = self._tf_buttons.get(tf)
        if btn:
            btn.setChecked(True)

    def set_strategies(self, names: list[str], strategy_tickers: dict[str, list[str]] | None = None) -> None:
        """Populate strategy combo box with ticker info.

        Args:
            names: Strategy names
            strategy_tickers: {strategy_name: [ticker1, ticker2, ...]} from config.yaml
        """
        self._strategy_combo.clear()
        self._strategy_combo.addItem("(none)", "")
        tmap = strategy_tickers or {}
        for name in sorted(names):
            tickers = tmap.get(name, [])
            if tickers:
                label = f"{name} ({', '.join(tickers)})"
            else:
                label = name
            self._strategy_combo.addItem(label, name)

    def get_selected_strategy(self) -> str:
        return self._strategy_combo.currentData() or ""

    def get_selected_days(self) -> int:
        return self._days_combo.currentData() or 14

    # ── Private ──────────────────────────────────────────

    def _on_tf_clicked(self, button: QPushButton) -> None:
        tf = button.property("timeframe")
        if tf:
            self.timeframe_changed.emit(tf)

    def _on_strategy_run(self) -> None:
        name = self.get_selected_strategy()
        if name:
            self.strategy_run_requested.emit(name)
