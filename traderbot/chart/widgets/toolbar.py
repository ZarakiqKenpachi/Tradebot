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
    tool_changed = pyqtSignal(str)               # drawing tool name or ""
    undo_drawing_requested = pyqtSignal()
    clear_drawings_requested = pyqtSignal()
    screenshot_requested = pyqtSignal()
    price_scale_changed = pyqtSignal(str)        # "normal", "log", "percentage"
    # Indicators
    bollinger_toggled = pyqtSignal(bool)
    rsi_toggled = pyqtSignal(bool)
    macd_toggled = pyqtSignal(bool)

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
        self._ema_check = QCheckBox("EMA")
        self._ema_check.setChecked(True)
        self._ema_check.toggled.connect(self.ema_toggled.emit)
        self.addWidget(self._ema_check)

        self._bb_check = QCheckBox("BB")
        self._bb_check.setToolTip("Bollinger Bands (20, 2)")
        self._bb_check.toggled.connect(self.bollinger_toggled.emit)
        self.addWidget(self._bb_check)

        self._rsi_check = QCheckBox("RSI")
        self._rsi_check.setToolTip("RSI (14)")
        self._rsi_check.toggled.connect(self.rsi_toggled.emit)
        self.addWidget(self._rsi_check)

        self._macd_check = QCheckBox("MACD")
        self._macd_check.setToolTip("MACD (12, 26, 9)")
        self._macd_check.toggled.connect(self.macd_toggled.emit)
        self.addWidget(self._macd_check)

        self.addSeparator()

        # ── Drawing tools ────────────────────────────────
        self._tool_buttons: dict[str, QPushButton] = {}
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(False)

        tools = [
            ("trendline", "Trend", "Trend line (two clicks)"),
            ("hray", "H-Ray", "Horizontal ray (one click)"),
            ("fib", "Fib", "Fibonacci retracement (two clicks)"),
            ("rect", "Rect", "Rectangle zone (two clicks)"),
            ("measure", "Ruler", "Measure distance (two clicks)"),
        ]
        for key, label, tip in tools:
            btn = QPushButton(label)
            btn.setCheckable(True)
            btn.setFixedWidth(50)
            btn.setToolTip(tip)
            btn.setProperty("tool_key", key)
            self._tool_group.addButton(btn)
            self._tool_buttons[key] = btn
            self.addWidget(btn)

        self._tool_group.buttonClicked.connect(self._on_tool_clicked)

        undo_btn = QPushButton("Undo")
        undo_btn.setFixedWidth(42)
        undo_btn.setToolTip("Undo last drawing (Ctrl+Z)")
        undo_btn.clicked.connect(self.undo_drawing_requested.emit)
        self.addWidget(undo_btn)

        clear_btn = QPushButton("X")
        clear_btn.setFixedWidth(24)
        clear_btn.setToolTip("Clear all drawings")
        clear_btn.clicked.connect(self.clear_drawings_requested.emit)
        self.addWidget(clear_btn)

        self.addSeparator()

        # ── Strategy selector ────────────────────────────
        self._strategy_combo = QComboBox()
        self._strategy_combo.setFixedWidth(180)
        self._strategy_combo.addItem("(none)", "")
        self.addWidget(self._strategy_combo)

        self._days_combo = QComboBox()
        self._days_combo.setFixedWidth(60)
        for d in [3, 7, 14, 30, 60, 90, 180, 365]:
            self._days_combo.addItem(f"{d}d", d)
        self._days_combo.setCurrentIndex(2)
        self.addWidget(self._days_combo)

        run_btn = QPushButton("Run")
        run_btn.setFixedWidth(40)
        run_btn.clicked.connect(self._on_strategy_run)
        self.addWidget(run_btn)

        self.addSeparator()

        # ── Controls ─────────────────────────────────────
        # Crosshair mode
        self._crosshair_btn = QPushButton("Magnet")
        self._crosshair_btn.setCheckable(True)
        self._crosshair_btn.setFixedWidth(55)
        self._crosshair_btn.setToolTip("Magnet crosshair")
        self._crosshair_btn.toggled.connect(self._on_crosshair_toggle)
        self.addWidget(self._crosshair_btn)

        # Price scale mode
        self._scale_combo = QComboBox()
        self._scale_combo.setFixedWidth(55)
        self._scale_combo.addItem("Auto", "normal")
        self._scale_combo.addItem("Log", "log")
        self._scale_combo.addItem("%", "percentage")
        self._scale_combo.currentIndexChanged.connect(self._on_scale_changed)
        self.addWidget(self._scale_combo)

        self._refresh_check = QCheckBox("Live")
        self._refresh_check.setChecked(True)
        self._refresh_check.setToolTip("Auto-refresh candles")
        self._refresh_check.toggled.connect(self.auto_refresh_toggled.emit)
        self.addWidget(self._refresh_check)

        fit_btn = QPushButton("Fit")
        fit_btn.setFixedWidth(32)
        fit_btn.clicked.connect(self.fit_requested.emit)
        self.addWidget(fit_btn)

        screenshot_btn = QPushButton("Snap")
        screenshot_btn.setFixedWidth(38)
        screenshot_btn.setToolTip("Save chart screenshot")
        screenshot_btn.clicked.connect(self.screenshot_requested.emit)
        self.addWidget(screenshot_btn)

        theme_btn = QPushButton("Theme")
        theme_btn.setFixedWidth(50)
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
        self.crosshair_mode_changed.emit("magnet" if checked else "normal")

    def _on_scale_changed(self, _index: int) -> None:
        mode = self._scale_combo.currentData() or "normal"
        self.price_scale_changed.emit(mode)

    def _on_tool_clicked(self, button: QPushButton) -> None:
        key = button.property("tool_key")
        if button.isChecked():
            # Uncheck all other tool buttons
            for k, b in self._tool_buttons.items():
                if k != key:
                    b.setChecked(False)
            self.tool_changed.emit(key)
        else:
            self.tool_changed.emit("")

    def deactivate_tools(self) -> None:
        """Called when JS cancels tool (Escape/RMB)."""
        for b in self._tool_buttons.values():
            b.setChecked(False)

    def _on_tf_clicked(self, button: QPushButton) -> None:
        tf = button.property("timeframe")
        if tf:
            self.timeframe_changed.emit(tf)

    def _on_strategy_run(self) -> None:
        name = self.get_selected_strategy()
        if name:
            self.strategy_run_requested.emit(name)
