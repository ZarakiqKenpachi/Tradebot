"""Toolbars — Main (symbol/TF/indicators/drawing) + Strategy (sim/playback)."""
from __future__ import annotations

from PyQt6.QtCore import pyqtSignal, Qt, QStringListModel, QTimer
from PyQt6.QtWidgets import (
    QToolBar, QPushButton, QLineEdit, QButtonGroup, QWidget, QHBoxLayout,
    QComboBox, QLabel, QCompleter,
)

from traderbot.chart.config import TIMEFRAMES


def _sep_label(text: str) -> QLabel:
    """Small muted group label for toolbar sections."""
    lbl = QLabel(text)
    lbl.setProperty("class", "toolbar-label")
    return lbl


def _icon_btn(text: str, width: int = 32, tip: str = "", checkable: bool = False, cls: str = "") -> QPushButton:
    btn = QPushButton(text)
    btn.setFixedWidth(width)
    if tip:
        btn.setToolTip(tip)
    if checkable:
        btn.setCheckable(True)
    if cls:
        btn.setProperty("class", cls)
    return btn


# ═══════════════════════════════════════════════════════════
# Main toolbar — symbol, timeframes, indicators, drawing, view
# ═══════════════════════════════════════════════════════════

class MainToolbar(QToolBar):
    """Top toolbar row: symbol | timeframe | indicators | drawing | view."""

    # Signals
    symbol_search_requested = pyqtSignal(str)
    symbol_quick_selected = pyqtSignal(str, str)
    timeframe_changed = pyqtSignal(str)
    ema_toggled = pyqtSignal(bool)
    auto_refresh_toggled = pyqtSignal(bool)
    theme_toggle_requested = pyqtSignal()
    fit_requested = pyqtSignal()
    crosshair_mode_changed = pyqtSignal(str)
    tool_changed = pyqtSignal(str)
    undo_drawing_requested = pyqtSignal()
    clear_drawings_requested = pyqtSignal()
    screenshot_requested = pyqtSignal()
    price_scale_changed = pyqtSignal(str)
    bollinger_toggled = pyqtSignal(bool)
    rsi_toggled = pyqtSignal(bool)
    macd_toggled = pyqtSignal(bool)

    def __init__(self, parent=None):
        super().__init__("Main", parent)
        self.setMovable(False)
        self.setFloatable(False)
        self.setObjectName("main-toolbar")
        self._symbol_map: dict[str, str] = {}

        # ── Symbol ───────────────────────────────────────
        self._ticker_combo = QComboBox()
        self._ticker_combo.setFixedWidth(130)
        self._ticker_combo.setProperty("class", "symbol-combo")
        self._ticker_combo.addItem("Tickers...", "")
        self._ticker_combo.currentIndexChanged.connect(self._on_ticker_combo_changed)
        self.addWidget(self._ticker_combo)

        self._symbol_input = QLineEdit()
        self._symbol_input.setPlaceholderText("Symbol...")
        self._symbol_input.setFixedWidth(160)
        self._symbol_input.setProperty("class", "symbol-input")
        self._symbol_input.returnPressed.connect(
            lambda: self.symbol_search_requested.emit(self._symbol_input.text())
        )

        self._completer_model = QStringListModel()
        self._completer = QCompleter(self._completer_model, self)
        self._completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        self._completer.setMaxVisibleItems(12)
        self._completer.setFilterMode(Qt.MatchFlag.MatchContains)
        self._completer.activated.connect(self._on_completer_activated)
        self._symbol_input.setCompleter(self._completer)

        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(300)
        self._search_timer.timeout.connect(self._on_search_debounced)
        self._symbol_input.textChanged.connect(lambda _: self._search_timer.start())

        self.addWidget(self._symbol_input)
        self.addSeparator()

        # ── Timeframe ────────────────────────────────────
        self._tf_group = QButtonGroup(self)
        self._tf_group.setExclusive(True)
        self._tf_buttons: dict[str, QPushButton] = {}

        for tf_key, tf_label in TIMEFRAMES:
            btn = QPushButton(tf_label)
            btn.setCheckable(True)
            btn.setFixedWidth(36)
            btn.setProperty("class", "tf-btn")
            btn.setProperty("timeframe", tf_key)
            self._tf_group.addButton(btn)
            self._tf_buttons[tf_key] = btn
            self.addWidget(btn)

        self._tf_group.buttonClicked.connect(self._on_tf_clicked)
        if "1h" in self._tf_buttons:
            self._tf_buttons["1h"].setChecked(True)

        self.addSeparator()

        # ── Indicators (toggle buttons, not checkboxes) ──
        self._ema_check = _icon_btn("EMA", 38, "EMA 20/50", checkable=True, cls="indicator-btn")
        self._ema_check.setChecked(True)
        self._ema_check.toggled.connect(self.ema_toggled.emit)
        self.addWidget(self._ema_check)

        self._bb_check = _icon_btn("BB", 30, "Bollinger Bands (20, 2)", checkable=True, cls="indicator-btn")
        self._bb_check.toggled.connect(self.bollinger_toggled.emit)
        self.addWidget(self._bb_check)

        self._rsi_check = _icon_btn("RSI", 34, "RSI (14)", checkable=True, cls="indicator-btn")
        self._rsi_check.toggled.connect(self.rsi_toggled.emit)
        self.addWidget(self._rsi_check)

        self._macd_check = _icon_btn("MACD", 42, "MACD (12, 26, 9)", checkable=True, cls="indicator-btn")
        self._macd_check.toggled.connect(self.macd_toggled.emit)
        self.addWidget(self._macd_check)

        self.addSeparator()

        # ── Drawing tools ────────────────────────────────
        self._tool_buttons: dict[str, QPushButton] = {}
        self._tool_group = QButtonGroup(self)
        self._tool_group.setExclusive(False)

        tools = [
            ("trendline", "/", 22, "Trend line"),
            ("hray", "--", 24, "Horizontal ray"),
            ("fib", "Fib", 30, "Fibonacci retracement"),
            ("rect", "[]", 24, "Rectangle zone"),
            ("measure", "Ru", 24, "Measure distance"),
        ]
        for key, label, w, tip in tools:
            btn = _icon_btn(label, w, tip, checkable=True, cls="tool-btn")
            btn.setProperty("tool_key", key)
            self._tool_group.addButton(btn)
            self._tool_buttons[key] = btn
            self.addWidget(btn)

        self._tool_group.buttonClicked.connect(self._on_tool_clicked)

        undo_btn = _icon_btn("Z", 22, "Undo drawing (Ctrl+Z)")
        undo_btn.clicked.connect(self.undo_drawing_requested.emit)
        self.addWidget(undo_btn)

        clear_btn = _icon_btn("X", 22, "Clear all drawings")
        clear_btn.clicked.connect(self.clear_drawings_requested.emit)
        self.addWidget(clear_btn)

        self.addSeparator()

        # ── View controls (right side) ───────────────────
        spacer = QWidget()
        spacer.setFixedWidth(0)
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy(), spacer.sizePolicy().verticalPolicy())
        self.addWidget(spacer)

        self._crosshair_btn = _icon_btn("Mag", 32, "Magnet crosshair", checkable=True, cls="view-btn")
        self._crosshair_btn.toggled.connect(self._on_crosshair_toggle)
        self.addWidget(self._crosshair_btn)

        self._scale_combo = QComboBox()
        self._scale_combo.setFixedWidth(50)
        self._scale_combo.setProperty("class", "view-combo")
        self._scale_combo.addItem("Auto", "normal")
        self._scale_combo.addItem("Log", "log")
        self._scale_combo.addItem("%", "percentage")
        self._scale_combo.currentIndexChanged.connect(self._on_scale_changed)
        self.addWidget(self._scale_combo)

        self._refresh_btn = _icon_btn("Live", 36, "Auto-refresh candles", checkable=True, cls="view-btn")
        self._refresh_btn.setChecked(True)
        self._refresh_btn.toggled.connect(self.auto_refresh_toggled.emit)
        self.addWidget(self._refresh_btn)

        fit_btn = _icon_btn("Fit", 28, "Fit chart to content", cls="view-btn")
        fit_btn.clicked.connect(self.fit_requested.emit)
        self.addWidget(fit_btn)

        snap_btn = _icon_btn("Cam", 32, "Save screenshot", cls="view-btn")
        snap_btn.clicked.connect(self.screenshot_requested.emit)
        self.addWidget(snap_btn)

        theme_btn = _icon_btn("T", 22, "Toggle dark/light theme", cls="view-btn")
        theme_btn.clicked.connect(self.theme_toggle_requested.emit)
        self.addWidget(theme_btn)

    # ── Public API ───────────────────────────────────────

    def set_symbol_text(self, text: str) -> None:
        self._symbol_input.setText(text)

    def set_timeframe(self, tf: str) -> None:
        btn = self._tf_buttons.get(tf)
        if btn:
            btn.setChecked(True)

    def set_ticker_list(self, tickers: list[tuple[str, str, str]]) -> None:
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
        self._search_service = search_service

    def deactivate_tools(self) -> None:
        for b in self._tool_buttons.values():
            b.setChecked(False)

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
        part = text.split("  ")[0].strip()
        if ":" in part:
            exchange, symbol = part.split(":", 1)
            self._symbol_input.setText(symbol)
            self.symbol_quick_selected.emit(symbol, exchange)
        else:
            self._symbol_input.setText(part)
            self.symbol_search_requested.emit(part)

    def _on_search_debounced(self) -> None:
        text = self._symbol_input.text().strip()
        if len(text) < 2:
            return
        svc = getattr(self, "_search_service", None)
        if svc is None:
            return
        try:
            results = svc.search(text, limit=12)
            completions = [f"{i.exchange}:{i.symbol}  {i.description}" for i in results]
            self._completer_model.setStringList(completions)
        except Exception:
            pass

    def _on_crosshair_toggle(self, checked: bool) -> None:
        self.crosshair_mode_changed.emit("magnet" if checked else "normal")

    def _on_scale_changed(self, _index: int) -> None:
        self.price_scale_changed.emit(self._scale_combo.currentData() or "normal")

    def _on_tool_clicked(self, button: QPushButton) -> None:
        key = button.property("tool_key")
        if button.isChecked():
            for k, b in self._tool_buttons.items():
                if k != key:
                    b.setChecked(False)
            self.tool_changed.emit(key)
        else:
            self.tool_changed.emit("")

    def _on_tf_clicked(self, button: QPushButton) -> None:
        tf = button.property("timeframe")
        if tf:
            self.timeframe_changed.emit(tf)


# ═══════════════════════════════════════════════════════════
# Strategy toolbar — simulation & playback (second row)
# ═══════════════════════════════════════════════════════════

class StrategyBar(QToolBar):
    """Second toolbar row: strategy selection + run/playback controls."""

    strategy_run_requested = pyqtSignal(str)
    run_all_requested = pyqtSignal(int)          # sim_days
    clear_trades_requested = pyqtSignal()
    playback_play_requested = pyqtSignal(str)
    playback_pause_requested = pyqtSignal()
    playback_stop_requested = pyqtSignal()
    playback_step_requested = pyqtSignal()
    playback_skip_to_end_requested = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__("Strategy", parent)
        self.setMovable(False)
        self.setFloatable(False)
        self.setObjectName("strategy-toolbar")

        self.addWidget(_sep_label("Strategy"))

        self._strategy_combo = QComboBox()
        self._strategy_combo.setFixedWidth(200)
        self._strategy_combo.setProperty("class", "strategy-combo")
        self._strategy_combo.addItem("Select strategy...", "")
        self.addWidget(self._strategy_combo)

        self._days_combo = QComboBox()
        self._days_combo.setFixedWidth(55)
        self._days_combo.setProperty("class", "days-combo")
        for d in [3, 7, 14, 30, 60, 90, 180, 365]:
            self._days_combo.addItem(f"{d}d", d)
        self._days_combo.setCurrentIndex(2)
        self.addWidget(self._days_combo)

        run_btn = _icon_btn("Run", 40, "Run simulation (instant)", cls="accent-btn")
        run_btn.clicked.connect(self._on_strategy_run)
        self.addWidget(run_btn)

        run_all_btn = _icon_btn("All", 36, "Run all config tickers (shared balance)", cls="accent-btn")
        run_all_btn.clicked.connect(self._on_run_all)
        self.addWidget(run_all_btn)

        self._clear_btn = _icon_btn("✕", 26, "Clear simulation trades", cls="stop-btn")
        self._clear_btn.clicked.connect(self.clear_trades_requested.emit)
        self._clear_btn.setEnabled(False)
        self.addWidget(self._clear_btn)

        self.addSeparator()

        # ── Playback ─────────────────────────────────────
        self.addWidget(_sep_label("Playback"))

        self._play_btn = _icon_btn("Play", 42, "Replay candle-by-candle", cls="play-btn")
        self._play_btn.clicked.connect(self._on_play)
        self.addWidget(self._play_btn)

        self._pause_btn = _icon_btn("||", 26, "Pause / Resume (Space)", cls="playback-btn")
        self._pause_btn.setEnabled(False)
        self._pause_btn.clicked.connect(self.playback_pause_requested.emit)
        self.addWidget(self._pause_btn)

        self._step_btn = _icon_btn(">|", 26, "Step +1 candle (Shift+Right)", cls="playback-btn")
        self._step_btn.setEnabled(False)
        self._step_btn.clicked.connect(self.playback_step_requested.emit)
        self.addWidget(self._step_btn)

        self._skip_btn = _icon_btn(">>|", 30, "Skip to end (no animation)", cls="playback-btn")
        self._skip_btn.setEnabled(False)
        self._skip_btn.clicked.connect(self.playback_skip_to_end_requested.emit)
        self.addWidget(self._skip_btn)

        self._stop_btn = _icon_btn("Stop", 36, "Stop playback", cls="stop-btn")
        self._stop_btn.setEnabled(False)
        self._stop_btn.clicked.connect(self.playback_stop_requested.emit)
        self.addWidget(self._stop_btn)

        self._speed_combo = QComboBox()
        self._speed_combo.setFixedWidth(52)
        self._speed_combo.setProperty("class", "speed-combo")
        for label, ms in [("1x", 500), ("2x", 250), ("5x", 100), ("10x", 50), ("Max", 5)]:
            self._speed_combo.addItem(label, ms)
        self._speed_combo.setCurrentIndex(2)
        self._speed_combo.setToolTip("Playback speed")
        self.addWidget(self._speed_combo)

    # ── Public API ───────────────────────────────────────

    def set_strategies(self, names: list[str], strategy_tickers: dict[str, list[str]] | None = None) -> None:
        self._strategy_combo.clear()
        self._strategy_combo.addItem("Select strategy...", "")
        tmap = strategy_tickers or {}
        for name in sorted(names):
            tickers = tmap.get(name, [])
            label = f"{name} ({', '.join(tickers)})" if tickers else name
            self._strategy_combo.addItem(label, name)

    def get_selected_strategy(self) -> str:
        return self._strategy_combo.currentData() or ""

    def get_selected_days(self) -> int:
        return self._days_combo.currentData() or 14

    def get_playback_speed(self) -> int:
        return self._speed_combo.currentData() or 100

    def set_playback_state(self, playing: bool) -> None:
        self._play_btn.setEnabled(not playing)
        self._pause_btn.setEnabled(playing)
        self._step_btn.setEnabled(playing)
        self._skip_btn.setEnabled(playing)
        self._stop_btn.setEnabled(playing)

    # ── Private ──────────────────────────────────────────

    def set_has_sim_trades(self, has: bool) -> None:
        """Enable/disable the clear button based on whether sim trades exist."""
        self._clear_btn.setEnabled(has)

    def _on_strategy_run(self) -> None:
        name = self.get_selected_strategy()
        if name:
            self.strategy_run_requested.emit(name)

    def _on_run_all(self) -> None:
        days = self.get_selected_days()
        self.run_all_requested.emit(days)

    def _on_play(self) -> None:
        name = self.get_selected_strategy()
        if name:
            self.playback_play_requested.emit(name)
