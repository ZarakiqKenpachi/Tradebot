"""Main toolbar — symbol search, timeframe buttons, indicators toggle."""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal, Qt, QStringListModel, QTimer
from PyQt6.QtWidgets import (
    QToolBar, QPushButton, QLineEdit, QButtonGroup, QWidget, QHBoxLayout,
    QCheckBox, QComboBox, QLabel, QCompleter,
)

from traderbot.chart.config import TIMEFRAMES


class MainToolbar(QToolBar):
    """Top toolbar with symbol search, timeframe selector, and controls."""

    # Signals
    symbol_search_requested = pyqtSignal(str)   # search query
    symbol_quick_selected = pyqtSignal(str, str) # symbol, exchange
    timeframe_changed = pyqtSignal(str)          # "1h", "4h", etc.
    ema_toggled = pyqtSignal(bool)               # show/hide EMA
    auto_refresh_toggled = pyqtSignal(bool)      # auto-refresh on/off
    theme_toggle_requested = pyqtSignal()        # switch dark/light
    strategy_run_requested = pyqtSignal(str)     # strategy name
    fit_requested = pyqtSignal()                 # fit chart to content
    crosshair_mode_changed = pyqtSignal(str)     # "normal" or "magnet"
    drawing_mode_toggled = pyqtSignal(bool)      # drawing mode on/off
    clear_lines_requested = pyqtSignal()         # clear user-drawn lines

    def __init__(self, parent=None):
        super().__init__("Main Toolbar", parent)
        self.setMovable(False)
        self.setFloatable(False)
        self._symbol_map: dict[str, str] = {}  # "MOEX:SBER" → exchange

        # ── Quick ticker selector ────────────────────────
        self._ticker_combo = QComboBox()
        self._ticker_combo.setFixedWidth(120)
        self._ticker_combo.addItem("Tickers...", "")
        self._ticker_combo.currentIndexChanged.connect(self._on_ticker_combo_changed)
        self.addWidget(self._ticker_combo)

        # ── Symbol search with autocomplete ──────────────
        self._symbol_input = QLineEdit()
        self._symbol_input.setPlaceholderText("Symbol (SBER, BTCUSDT...)")
        self._symbol_input.setFixedWidth(200)
        self._symbol_input.returnPressed.connect(
            lambda: self.symbol_search_requested.emit(self._symbol_input.text())
        )

        # Autocomplete model — updated dynamically as user types
        self._completer_model = QStringListModel()
        self._completer = QCompleter(self._completer_model, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setMaxVisibleItems(12)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.activated.connect(self._on_completer_activated)
        self._symbol_input.setCompleter(self._completer)

        # Debounce timer for live search
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._on_search_debounced)
        self._symbol_input.textChanged.connect(lambda _: self._search_timer.start())

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

        # ── Chart tools ──────────────────────────────────
        self.addWidget(QLabel(" Tools: "))

        self._crosshair_btn = QPushButton("Crosshair")
        self._crosshair_btn.setCheckable(True)
        self._crosshair_btn.setFixedWidth(75)
        self._crosshair_btn.setToolTip("Toggle magnet crosshair")
        self._crosshair_btn.toggled.connect(self._on_crosshair_toggle)
        self.addWidget(self._crosshair_btn)

        self._draw_btn = QPushButton("H-Line")
        self._draw_btn.setCheckable(True)
        self._draw_btn.setFixedWidth(55)
        self._draw_btn.setToolTip("Click on chart to draw horizontal line")
        self._draw_btn.toggled.connect(self.drawing_mode_toggled.emit)
        self.addWidget(self._draw_btn)

        self._clear_lines_btn = QPushButton("Clear")
        self._clear_lines_btn.setFixedWidth(45)
        self._clear_lines_btn.setToolTip("Remove all user-drawn lines")
        self._clear_lines_btn.clicked.connect(self.clear_lines_requested.emit)
        self.addWidget(self._clear_lines_btn)

        self.addSeparator()

        # ── Controls ─────────────────────────────────────
        spacer = QWidget()
        spacer.setFixedWidth(10)
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

    def set_ticker_list(self, tickers: list[tuple[str, str, str]]) -> None:
        """Populate quick ticker combo and autocomplete.

        Args:
            tickers: [(symbol, exchange, description), ...]
        """
        self._ticker_combo.blockSignals(True)
        self._ticker_combo.clear()
        self._ticker_combo.addItem("Tickers...", "")
        self._symbol_map.clear()

        completions = []
        for symbol, exchange, desc in tickers:
            label = f"{symbol}" if not desc else f"{symbol} — {desc}"
            key = f"{exchange}:{symbol}"
            self._ticker_combo.addItem(label, key)
            self._symbol_map[key] = exchange
            completions.append(f"{exchange}:{symbol}  {desc}")
            completions.append(symbol)

        self._completer_model.setStringList(completions)
        self._ticker_combo.blockSignals(False)

    def set_search_service(self, search_service) -> None:
        """Set search service for live autocomplete."""
        self._search_service = search_service

    # ── Private ──────────────────────────────────────────

    def _on_ticker_combo_changed(self, index: int) -> None:
        key = self._ticker_combo.currentData()
        if not key:
            return
        parts = key.split(":", 1)
        if len(parts) == 2:
            exchange, symbol = parts
            self._symbol_input.setText(symbol)
            self.symbol_quick_selected.emit(symbol, exchange)

    def _on_completer_activated(self, text: str) -> None:
        """User selected an autocomplete suggestion."""
        # Parse "MOEX:SBER  Sberbank" or just "SBER"
        part = text.split("  ")[0].strip()
        if ":" in part:
            exchange, symbol = part.split(":", 1)
            self._symbol_input.setText(symbol)
            self.symbol_quick_selected.emit(symbol, exchange)
        else:
            self._symbol_input.setText(part)
            self.symbol_search_requested.emit(part)

    def _on_search_debounced(self) -> None:
        """Live autocomplete: update suggestions as user types."""
        text = self._symbol_input.text().strip()
        if len(text) < 2:
            return
        svc = getattr(self, "_search_service", None)
        if svc is None:
            return
        try:
            results = svc.search(text, limit=12)
            completions = []
            for info in results:
                completions.append(f"{info.exchange}:{info.symbol}  {info.description}")
            self._completer_model.setStringList(completions)
        except Exception:
            pass

    def _on_crosshair_toggle(self, checked: bool) -> None:
        mode = "magnet" if checked else "normal"
        self.crosshair_mode_changed.emit(mode)

    def _on_tf_clicked(self, button: QPushButton) -> None:
        tf = button.property("timeframe")
        if tf:
            self.timeframe_changed.emit(tf)

    def _on_strategy_run(self) -> None:
        name = self.get_selected_strategy()
        if name:
            self.strategy_run_requested.emit(name)
