"""Chart Analyzer — Desktop application entry point.

Usage:
    py -3.12 -m traderbot.chart.app
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
import json
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QSplitter, QVBoxLayout, QWidget,
    QMessageBox,
)

from traderbot.chart.config import (
    AppConfig, DARK_THEME, LIGHT_THEME, ChartTheme,
)
from traderbot.chart.data.cache import CandleCache
from traderbot.chart.data.tvfeed import TvDatafeedProvider
from traderbot.chart.data.symbol_search import SymbolSearchService
from traderbot.chart.chart.widget import ChartWidget
from traderbot.chart.trades.journal import TradeJournalReader
from traderbot.chart.trades.sim_journal import SimulationJournal
from traderbot.chart.trades.table import TradesPanel
from traderbot.chart.strategy.runner import StrategyRunner, SimulationConfig
from traderbot.chart.widgets.toolbar import MainToolbar, StrategyBar
from traderbot.chart.widgets.symbol_dialog import SymbolSearchDialog
from traderbot.chart.widgets.trade_detail import TradeDetailDialog
from traderbot.chart.widgets.statusbar import ChartStatusBar

logger = logging.getLogger(__name__)

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STYLE_PATH = Path(__file__).parent / "resources" / "style.qss"
DB_PATH = PROJECT_ROOT / "traderbot" / "data" / "traderbot.db"


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self, config: AppConfig | None = None):
        super().__init__()
        self._config = config or AppConfig()
        self._is_dark = True

        self.setWindowTitle("Chart Analyzer — TraderBot")
        self.setMinimumSize(1000, 600)
        self.resize(self._config.window_width, self._config.window_height)

        # ── Services ─────────────────────────────────────
        self._provider = TvDatafeedProvider(
            username=self._config.tv_username or None,
            password=self._config.tv_password or None,
            auth_token=self._config.tv_auth_token or None,
        )
        self._cache = CandleCache(str(PROJECT_ROOT / self._config.cache_db_path))
        self._search_service = SymbolSearchService(
            self._provider,
            history_path=PROJECT_ROOT / "data" / "symbol_history.json",
        )
        self._journal = TradeJournalReader(str(DB_PATH) if DB_PATH.exists() else None)
        self._sim_journal = SimulationJournal(str(PROJECT_ROOT / "data" / "sim_journal.db"))
        self._strategy_runner = StrategyRunner(provider=self._provider)

        # ── Toolbars ─────────────────────────────────────
        self._toolbar = MainToolbar(self)
        self._toolbar.set_search_service(self._search_service)
        self._populate_ticker_list()
        self.addToolBar(self._toolbar)

        self._strategy_bar = StrategyBar(self)
        self._strategy_bar.set_strategies(
            self._strategy_runner.get_strategy_names(),
            strategy_tickers=self._load_strategy_tickers(),
        )
        self.addToolBar(self._strategy_bar)

        # ── Central widget ─────────────���─────────────────
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(0, 0, 0, 0)

        # Splitter: chart (top) + trades table (bottom)
        splitter = QSplitter(Qt.Orientation.Vertical)

        # Chart
        self._chart = ChartWidget(
            provider=self._provider,
            cache=self._cache,
            theme=self._config.theme,
            parent=self,
        )
        splitter.addWidget(self._chart)

        # Trades panel
        self._trades_panel = TradesPanel(self)
        splitter.addWidget(self._trades_panel)

        # 70/30 split
        splitter.setStretchFactor(0, 7)
        splitter.setStretchFactor(1, 3)

        layout.addWidget(splitter)

        # ── Status bar ───────���───────────────────────────
        self._statusbar = ChartStatusBar(self)
        self.setStatusBar(self._statusbar)

        # ── Playback state ───────────────────────────────
        self._playback_timer = QTimer(self)
        self._playback_timer.timeout.connect(self._playback_tick)
        self._pb_candles: list[dict] = []
        self._pb_trades: list = []
        self._pb_idx = 0
        self._pb_warmup = 60
        self._pb_trade_ptr = 0      # next trade entry to show
        self._pb_active_trade = None # trade with open position (lines shown)
        self._pb_shown_markers: list[dict] = []  # accumulated markers
        self._pb_shown_trades: list = []  # trades for table
        self._pb_paused = False

        # ── Connect signals ──────────────────────────────
        self._connect_signals()

        # ── Apply style ───────────���──────────────────────
        self._apply_stylesheet()

        # ── Load default symbol after a short delay ──────
        QTimer.singleShot(500, self._load_default)

    def _connect_signals(self) -> None:
        # Toolbar
        self._toolbar.symbol_search_requested.connect(self._on_symbol_search)
        self._toolbar.symbol_quick_selected.connect(self._load_symbol)
        self._toolbar.timeframe_changed.connect(self._on_timeframe_changed)
        self._toolbar.ema_toggled.connect(self._chart.toggle_ema)
        self._toolbar.auto_refresh_toggled.connect(self._chart.set_auto_refresh)
        self._toolbar.theme_toggle_requested.connect(self._toggle_theme)
        self._toolbar.fit_requested.connect(self._chart.fit_content)
        self._toolbar.crosshair_mode_changed.connect(self._chart.set_crosshair_mode)
        self._toolbar.tool_changed.connect(self._chart.set_active_tool)
        self._toolbar.undo_drawing_requested.connect(self._chart.undo_drawing)
        self._toolbar.clear_drawings_requested.connect(self._chart.clear_all_drawings)
        self._toolbar.screenshot_requested.connect(self._chart.take_screenshot)
        self._toolbar.price_scale_changed.connect(self._chart.set_price_scale_mode)
        self._toolbar.bollinger_toggled.connect(lambda v: self._chart.set_bollinger(visible=v))
        self._toolbar.rsi_toggled.connect(lambda v: self._chart.set_rsi(visible=v))
        self._toolbar.macd_toggled.connect(lambda v: self._chart.set_macd(visible=v))

        # Strategy bar
        self._strategy_bar.strategy_run_requested.connect(self._on_strategy_run)
        self._strategy_bar.playback_play_requested.connect(self._on_playback_start)
        self._strategy_bar.playback_pause_requested.connect(self._on_playback_pause)
        self._strategy_bar.playback_stop_requested.connect(self._on_playback_stop)
        self._strategy_bar.playback_step_requested.connect(self._on_playback_step)
        self._strategy_bar._speed_combo.currentIndexChanged.connect(self._on_playback_speed_changed)

        # Chart
        self._chart.status_changed.connect(self._statusbar.set_status)
        self._chart.crosshair_data.connect(self._statusbar.set_ohlcv)
        self._chart.trade_marker_clicked.connect(self._on_marker_clicked)
        self._chart.candles_loaded.connect(self._on_candles_loaded)
        self._chart._bridge.tool_deactivated.connect(self._toolbar.deactivate_tools)
        self._chart._bridge.playback_pause.connect(self._on_playback_pause)
        self._chart._bridge.playback_step.connect(self._on_playback_step)

        # Trades table
        self._trades_panel.trade_selected.connect(self._on_trade_selected)
        self._trades_panel.trade_double_clicked.connect(self._on_trade_double_clicked)
        self._trades_panel.scroll_to_trade.connect(self._on_scroll_to_trade)

    # ── Event handlers ───────────���───────────────────────

    def _load_default(self) -> None:
        """Load default symbol on startup."""
        symbol = self._config.default_symbol
        tf = self._config.default_timeframe
        self._toolbar.set_symbol_text(symbol)
        self._toolbar.set_timeframe(tf)

        # Use known MOEX exchange for default
        from traderbot.chart.data.symbol_search import _KNOWN_MOEX
        exchange = _KNOWN_MOEX.get(symbol, ("MOEX",))[0]

        self._chart.load_symbol(symbol, exchange, tf)
        self._statusbar.set_connected(self._provider.is_connected())
        self._load_trades_for_ticker(symbol)

    def _on_symbol_search(self, query: str) -> None:
        """Open symbol search dialog or quick-load known symbol."""
        query = query.strip().upper()
        if not query:
            return

        # Quick load if exact match in known symbols
        from traderbot.chart.data.symbol_search import _KNOWN_MOEX
        if query in _KNOWN_MOEX:
            exchange = _KNOWN_MOEX[query][0]
            self._load_symbol(query, exchange)
            return

        # Open search dialog
        dialog = SymbolSearchDialog(self._search_service, self)
        dialog._input.setText(query)
        dialog._on_search(query)

        def on_selected(info):
            self._load_symbol(info.symbol, info.exchange)

        dialog.symbol_selected.connect(on_selected)
        dialog.exec()

    def _load_symbol(self, symbol: str, exchange: str) -> None:
        tf = self._chart.current_timeframe or self._config.default_timeframe
        self._toolbar.set_symbol_text(symbol)
        self._chart.load_symbol(symbol, exchange, tf)
        self._statusbar.set_connected(self._provider.is_connected())
        self._load_trades_for_ticker(symbol)

    def _on_timeframe_changed(self, tf: str) -> None:
        self._chart.change_timeframe(tf)

    def _on_candles_loaded(self, count: int) -> None:
        """After candles load, recompute all active indicators."""
        self._chart.set_ema(20, 50, visible=self._toolbar._ema_check.isChecked())
        if self._toolbar._bb_check.isChecked():
            self._chart.set_bollinger(visible=True)
        if self._toolbar._rsi_check.isChecked():
            self._chart.set_rsi(visible=True)
        if self._toolbar._macd_check.isChecked():
            self._chart.set_macd(visible=True)

    def _on_marker_clicked(self, marker_data: dict) -> None:
        """Show trade detail when marker clicked on chart."""
        dialog = TradeDetailDialog(marker_data, self)
        dialog.exec()

    def _on_trade_selected(self, trade: dict) -> None:
        """Highlight trade on chart when row clicked in table."""
        self._chart.highlight_trade(trade)

    def _on_trade_double_clicked(self, trade: dict) -> None:
        """Show trade detail popup on double-click."""
        dialog = TradeDetailDialog(trade, self)
        dialog.exec()

    def _on_scroll_to_trade(self, trade: dict) -> None:
        """Right-click → Show on chart: scroll + highlight SL/TP lines."""
        self._chart.highlight_trade(trade)

    def _on_strategy_run(self, strategy_name: str) -> None:
        """Run full strategy simulation with 15S precision."""
        df = self._chart.get_candle_df()
        if df.empty:
            QMessageBox.warning(self, "No Data", "Load candles first.")
            return

        sim_days = self._strategy_bar.get_selected_days()
        self._statusbar.set_status(f"Running '{strategy_name}' for {sim_days} days...")

        try:
            self._run_strategy_inner(strategy_name, df, sim_days)
        except Exception:
            logger.exception("[APP] Strategy run failed")
            QMessageBox.critical(self, "Error", f"Strategy run failed:\n{logger.name}")
            import traceback
            QMessageBox.critical(self, "Strategy Error", traceback.format_exc()[-500:])
            self._statusbar.set_status("Strategy run failed — see error dialog")

    def _run_strategy_inner(self, strategy_name: str, df, sim_days: int) -> None:
        """Inner logic for strategy run (separated so errors are caught)."""
        import pandas as pd
        from datetime import timedelta

        # 1. Build candle dict with all required timeframes
        strategy_cls = self._strategy_runner._registry.get(strategy_name)
        if not strategy_cls:
            QMessageBox.warning(self, "Error", f"Strategy '{strategy_name}' not found")
            return

        required = getattr(strategy_cls, "required_timeframes", [])
        candles: dict = {}

        # Load each required timeframe from TV (full depth)
        for tf in required:
            self._statusbar.set_status(f"Loading {tf} data for simulation...")
            try:
                tf_df = self._provider.get_candles(
                    self._chart.current_symbol, self._chart.current_exchange,
                    tf, n_bars=5000,
                )
                if not tf_df.empty:
                    candles[tf] = tf_df
            except Exception:
                pass

            # Fallback: resample from what we have
            if tf not in candles and not df.empty:
                resampled = self._resample_candles(df, tf)
                if resampled is not None and not resampled.empty:
                    candles[tf] = resampled

        if not candles:
            QMessageBox.warning(self, "Error", "Could not load required timeframes")
            return

        # Trim candles to selected number of days
        cutoff = pd.Timestamp.now(tz="UTC") - timedelta(days=sim_days)
        for tf_key in list(candles.keys()):
            candles[tf_key] = candles[tf_key].loc[candles[tf_key].index >= cutoff]

        # 2. Load high-resolution scan data (15S if Premium, else 1m)
        scan_df = None
        scan_tf = "15S" if self._provider.has_premium else "1m"
        self._statusbar.set_status(f"Loading {scan_tf} scan data for precise SL/TP...")
        try:
            scan_df = self._provider.get_candles(
                self._chart.current_symbol, self._chart.current_exchange,
                scan_tf, n_bars=5000,
            )
            if scan_df.empty:
                scan_df = None
        except Exception:
            logger.warning("[APP] Could not load %s scan data, simulation will be less precise", scan_tf)

        # Trim scan data to the same period
        if scan_df is not None:
            scan_df = scan_df.loc[scan_df.index >= cutoff]
            if scan_df.empty:
                scan_df = None

        # 3. Run simulation
        self._statusbar.set_status(f"Simulating '{strategy_name}' ({sim_days}d)...")

        sim_config = SimulationConfig(scan_tf=scan_tf)
        result = self._strategy_runner.run(
            strategy_name, candles,
            scan_df=scan_df,
            config=sim_config,
            ticker=self._chart.current_symbol,
        )

        if result.errors:
            QMessageBox.warning(
                self, "Strategy Errors",
                "\n".join(result.errors[:10]),
            )
            if not result.trades:
                return

        # 4. Display results
        self._trades_panel.set_trades(result.trades)
        trade_dicts = [t.to_dict() for t in result.trades]
        self._chart.set_trade_markers(trade_dicts)

        # 5. Save to simulation journal
        run_id = self._sim_journal.save_run(
            strategy=strategy_name,
            ticker=self._chart.current_symbol,
            exchange=self._chart.current_exchange,
            timeframe=self._chart.current_timeframe,
            scan_tf=scan_tf,
            initial_balance=sim_config.initial_balance,
            final_balance=result.final_balance,
            total_pnl=result.total_pnl,
            max_drawdown=result.max_drawdown,
            setups_found=result.setups_found,
            trades=result.trades,
        )

        # Status with full stats
        precision = "15S" if scan_df is not None and scan_tf == "15S" else scan_tf if scan_df is not None else "bar-level"
        pnl_sign = "+" if result.total_pnl >= 0 else ""
        self._statusbar.set_status(
            f"Run #{run_id} | {strategy_name} {sim_days}d: {result.setups_found} setups, {len(result.trades)} trades | "
            f"P&L: {pnl_sign}{result.total_pnl:.2f} | DD: {result.max_drawdown*100:.1f}% | "
            f"Balance: {result.final_balance:.0f} | Precision: {precision}"
        )

    # ── Playback (candle-by-candle replay) ─────────────

    def _on_playback_start(self, strategy_name: str) -> None:
        """Run simulation, then replay candles one-by-one."""
        df = self._chart.get_candle_df()
        if df.empty:
            QMessageBox.warning(self, "No Data", "Load candles first.")
            return

        sim_days = self._strategy_bar.get_selected_days()
        self._statusbar.set_status(f"Preparing playback: '{strategy_name}' {sim_days}d...")

        try:
            result, candles, primary_tf = self._run_simulation_for_playback(
                strategy_name, df, sim_days,
            )
        except Exception:
            import traceback
            logger.exception("[APP] Playback simulation failed")
            QMessageBox.critical(self, "Error", traceback.format_exc()[-500:])
            return

        if not candles or result is None:
            return

        primary_df = candles[primary_tf]
        self._pb_candles = self._df_to_candle_list(primary_df)
        self._pb_trades = sorted(result.trades, key=lambda t: t.entry_time)
        self._pb_trade_ptr = 0
        self._pb_active_trade = None
        self._pb_shown_markers = []
        self._pb_shown_trades = []
        self._pb_paused = False
        self._pb_strategy_name = strategy_name

        # Calculate warmup: end BEFORE the first trade so markers
        # don't appear retroactively on warmup candles.
        self._pb_warmup = min(60, len(self._pb_candles) // 4)
        if self._pb_trades:
            first_entry_ts = self._trade_time_to_ts(self._pb_trades[0].entry_time)
            if first_entry_ts is not None:
                for i, c in enumerate(self._pb_candles):
                    if c["time"] >= first_entry_ts:
                        # Stop warmup 5 candles before the first trade
                        self._pb_warmup = max(0, i - 5)
                        break

        # Clear chart and load warmup candles
        warmup_data = self._pb_candles[:self._pb_warmup]
        if warmup_data:
            self._chart._bridge.set_candles(json.dumps(warmup_data))
        else:
            # No warmup — start with an empty chart, first candle via append
            self._chart._bridge.set_candles(json.dumps([self._pb_candles[0]]))
            self._pb_warmup = 1
        self._chart._bridge.set_trade_markers(json.dumps([]))
        self._chart._bridge.clear_price_lines()
        self._trades_panel.set_trades([])

        self._pb_idx = self._pb_warmup

        logger.info(
            "[PLAYBACK] %s: %d candles, %d trades, warmup=%d, first_trade=%s",
            strategy_name, len(self._pb_candles), len(self._pb_trades),
            self._pb_warmup,
            self._pb_trades[0].entry_time if self._pb_trades else "none",
        )

        # Start timer
        speed_ms = self._strategy_bar.get_playback_speed()
        self._playback_timer.start(speed_ms)
        self._strategy_bar.set_playback_state(True)
        self._statusbar.set_status(
            f"Playback: {strategy_name} | {len(self._pb_trades)} trades to replay"
        )

    def _run_simulation_for_playback(self, strategy_name: str, df, sim_days: int):
        """Run simulation and return (result, candles_dict, primary_tf)."""
        import pandas as pd
        from datetime import timedelta
        from traderbot.chart.strategy.runner import SimulationConfig

        strategy_cls = self._strategy_runner._registry.get(strategy_name)
        if not strategy_cls:
            QMessageBox.warning(self, "Error", f"Strategy '{strategy_name}' not found")
            return None, None, None

        required = getattr(strategy_cls, "required_timeframes", [])
        candles: dict = {}

        for tf in required:
            self._statusbar.set_status(f"Loading {tf} data...")
            try:
                tf_df = self._provider.get_candles(
                    self._chart.current_symbol, self._chart.current_exchange,
                    tf, n_bars=5000,
                )
                if not tf_df.empty:
                    candles[tf] = tf_df
            except Exception:
                pass
            if tf not in candles and not df.empty:
                resampled = self._resample_candles(df, tf)
                if resampled is not None and not resampled.empty:
                    candles[tf] = resampled

        if not candles:
            QMessageBox.warning(self, "Error", "Could not load required timeframes")
            return None, None, None

        cutoff = pd.Timestamp.now(tz="UTC") - timedelta(days=sim_days)
        for tf_key in list(candles.keys()):
            candles[tf_key] = candles[tf_key].loc[candles[tf_key].index >= cutoff]

        # Scan data for SL/TP precision
        scan_df = None
        scan_tf = "15S" if self._provider.has_premium else "1m"
        self._statusbar.set_status(f"Loading {scan_tf} scan data...")
        try:
            scan_df = self._provider.get_candles(
                self._chart.current_symbol, self._chart.current_exchange,
                scan_tf, n_bars=5000,
            )
            if scan_df.empty:
                scan_df = None
        except Exception:
            pass

        if scan_df is not None:
            scan_df = scan_df.loc[scan_df.index >= cutoff]
            if scan_df.empty:
                scan_df = None

        self._statusbar.set_status(f"Simulating '{strategy_name}'...")
        sim_config = SimulationConfig(scan_tf=scan_tf)
        result = self._strategy_runner.run(
            strategy_name, candles,
            scan_df=scan_df,
            config=sim_config,
            ticker=self._chart.current_symbol,
        )

        primary_tf = required[0] if required else list(candles.keys())[0]
        primary_df = candles[primary_tf]
        logger.info(
            "[PLAYBACK] Data range: %s .. %s (%d bars, tf=%s)",
            primary_df.index[0], primary_df.index[-1],
            len(primary_df), primary_tf,
        )
        return result, candles, primary_tf

    def _playback_tick(self) -> None:
        """Add one candle and check for trade events."""
        if self._pb_idx >= len(self._pb_candles):
            self._on_playback_stop()
            return

        candle = self._pb_candles[self._pb_idx]
        self._pb_idx += 1
        candle_time = candle["time"]

        # Append candle to chart and keep it in view
        self._chart._bridge.append_candles(json.dumps([candle]))
        self._chart._bridge.scroll_to_realtime()

        # Check trade entries
        while self._pb_trade_ptr < len(self._pb_trades):
            trade = self._pb_trades[self._pb_trade_ptr]
            entry_ts = self._trade_time_to_ts(trade.entry_time)
            if entry_ts is not None and entry_ts <= candle_time:
                # Show entry marker
                self._pb_shown_markers.append({
                    "time": entry_ts,
                    "type": "entry",
                    "direction": trade.direction,
                    "price": trade.entry_price,
                    "stop_price": trade.stop_price,
                    "target_price": trade.target_price,
                    "entry_reason": trade.entry_reason,
                    "qty": trade.qty,
                    "trade_id": trade.id,
                    "ticker": trade.ticker,
                })
                self._chart._bridge.set_trade_markers(
                    json.dumps(self._pb_shown_markers)
                )
                # Show SL/TP/Entry price lines
                self._pb_active_trade = trade
                self._show_position_lines(trade)
                self._pb_trade_ptr += 1
            else:
                break

        # Check trade exits
        if self._pb_active_trade is not None:
            trade = self._pb_active_trade
            exit_ts = self._trade_time_to_ts(trade.exit_time)
            if exit_ts is not None and exit_ts <= candle_time:
                # Show exit marker
                self._pb_shown_markers.append({
                    "time": exit_ts,
                    "type": "exit",
                    "direction": trade.direction,
                    "price": trade.exit_price,
                    "pnl": trade.pnl,
                    "exit_reason": trade.exit_reason,
                    "entry_reason": trade.entry_reason,
                    "candles_held": trade.candles_held,
                    "trade_id": trade.id,
                    "ticker": trade.ticker,
                })
                self._chart._bridge.set_trade_markers(
                    json.dumps(self._pb_shown_markers)
                )
                # Remove position lines
                self._chart._bridge.clear_price_lines()
                # Add to trades table
                self._pb_shown_trades.append(trade)
                self._trades_panel.set_trades(self._pb_shown_trades)
                self._pb_active_trade = None

        # Status
        pct = int(self._pb_idx / len(self._pb_candles) * 100)
        total_pnl = sum(t.pnl for t in self._pb_shown_trades)
        pnl_sign = "+" if total_pnl >= 0 else ""
        self._statusbar.set_status(
            f"Playback {pct}% | {self._pb_idx}/{len(self._pb_candles)} | "
            f"trades: {len(self._pb_shown_trades)} | P&L: {pnl_sign}{total_pnl:.2f}"
        )

    def _show_position_lines(self, trade) -> None:
        """Show entry/SL/TP price lines for an open position."""
        is_buy = trade.direction == "BUY"
        lines = [
            {
                "price": trade.entry_price,
                "color": "#26a69a" if is_buy else "#ef5350",
                "title": f"Entry {trade.entry_price:.2f}",
                "style": 0,
            },
            {
                "price": trade.stop_price,
                "color": "#ff9800",
                "title": f"SL {trade.stop_price:.2f}",
                "style": 2,
            },
            {
                "price": trade.target_price,
                "color": "#2962ff",
                "title": f"TP {trade.target_price:.2f}",
                "style": 2,
            },
        ]
        self._chart._bridge.set_price_lines(json.dumps(lines))

    def _on_playback_pause(self) -> None:
        """Toggle pause/resume."""
        if self._pb_paused:
            speed_ms = self._strategy_bar.get_playback_speed()
            self._playback_timer.start(speed_ms)
            self._pb_paused = False
        else:
            self._playback_timer.stop()
            self._pb_paused = True

    def _on_playback_step(self) -> None:
        """Advance one candle while paused."""
        if not self._pb_paused:
            self._playback_timer.stop()
            self._pb_paused = True
        self._playback_tick()

    def _on_playback_stop(self) -> None:
        """Stop playback and show final results."""
        self._playback_timer.stop()
        self._pb_paused = False
        self._strategy_bar.set_playback_state(False)
        self._chart._bridge.clear_price_lines()

        # Show ALL candles (including ones not yet played) so there's no gap
        if self._pb_candles:
            self._chart._bridge.set_candles(json.dumps(self._pb_candles))
            # Re-apply all accumulated markers
            if self._pb_shown_markers:
                self._chart._bridge.set_trade_markers(
                    json.dumps(self._pb_shown_markers)
                )

        total_pnl = sum(t.pnl for t in self._pb_shown_trades)
        pnl_sign = "+" if total_pnl >= 0 else ""
        self._statusbar.set_status(
            f"Playback done | {len(self._pb_shown_trades)} trades | "
            f"P&L: {pnl_sign}{total_pnl:.2f}"
        )

    def _on_playback_speed_changed(self, _index: int) -> None:
        """Update timer interval when speed changes during playback."""
        if self._playback_timer.isActive():
            speed_ms = self._strategy_bar.get_playback_speed()
            self._playback_timer.setInterval(speed_ms)

    @staticmethod
    def _df_to_candle_list(df) -> list[dict]:
        """Convert DataFrame to list of candle dicts for chart JS."""
        data = []
        for ts, row in df.iterrows():
            data.append({
                "time": int(ts.timestamp()),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
                "volume": float(row.get("volume", 0)),
            })
        return data

    @staticmethod
    def _trade_time_to_ts(time_str: str) -> int | None:
        """Convert trade time string to unix timestamp."""
        if not time_str:
            return None
        try:
            from datetime import datetime
            dt = datetime.fromisoformat(time_str)
            return int(dt.timestamp())
        except (ValueError, TypeError):
            return None

    def _load_trades_for_ticker(self, ticker: str) -> None:
        """Load trades from journal for the current ticker."""
        trades = self._journal.read_from_sqlite(ticker=ticker, limit=200)
        if trades:
            self._trades_panel.set_trades(trades)
            trade_dicts = [t.to_dict() for t in trades]
            self._chart.set_trade_markers(trade_dicts)

    def _populate_ticker_list(self) -> None:
        """Fill quick ticker combo with config tickers + known MOEX tickers."""
        from traderbot.chart.data.symbol_search import _KNOWN_MOEX

        tickers: list[tuple[str, str, str]] = []
        seen = set()

        # 1. Tickers from config.yaml (trading tickers first)
        config_path = PROJECT_ROOT / "traderbot" / "config.yaml"
        if config_path.exists():
            try:
                import yaml
                with open(config_path, "r", encoding="utf-8") as f:
                    cfg = yaml.safe_load(f)
                for ticker_name in cfg.get("tickers", {}):
                    if ticker_name not in seen:
                        exchange = _KNOWN_MOEX.get(ticker_name, ("MOEX",))[0]
                        desc = _KNOWN_MOEX.get(ticker_name, ("", ""))[1] if ticker_name in _KNOWN_MOEX else ""
                        tickers.append((ticker_name, exchange, desc))
                        seen.add(ticker_name)
            except Exception:
                pass

        # 2. All known MOEX tickers
        for sym, (exch, desc, _) in _KNOWN_MOEX.items():
            if sym not in seen:
                tickers.append((sym, exch, desc))
                seen.add(sym)

        self._toolbar.set_ticker_list(tickers)

    @staticmethod
    def _load_strategy_tickers() -> dict[str, list[str]]:
        """Load strategy→tickers mapping from config.yaml."""
        config_path = PROJECT_ROOT / "traderbot" / "config.yaml"
        if not config_path.exists():
            return {}
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            result: dict[str, list[str]] = {}
            for ticker_name, ticker_conf in cfg.get("tickers", {}).items():
                strategy = ticker_conf.get("strategy", "")
                if strategy:
                    result.setdefault(strategy, []).append(ticker_name)
            return result
        except Exception:
            return {}

    def _toggle_theme(self) -> None:
        """Switch between dark and light themes."""
        self._is_dark = not self._is_dark
        theme = DARK_THEME if self._is_dark else LIGHT_THEME
        self._config.theme = theme
        self._chart.set_theme(theme)
        self._apply_stylesheet()

    def _apply_stylesheet(self) -> None:
        """Load and apply QSS stylesheet."""
        if STYLE_PATH.exists():
            style = STYLE_PATH.read_text(encoding="utf-8")
            if not self._is_dark:
                # Dark → Light color mapping
                replacements = [
                    ("#131722", "#ffffff"),   # main bg
                    ("#1a1e28", "#f5f5f5"),   # secondary bg
                    ("#1e222d", "#f0f3fa"),   # toolbar bg
                    ("#252830", "#e0e3eb"),   # subtle borders
                    ("#2a2e39", "#d6dcde"),   # borders
                    ("#363a45", "#c8c8c8"),   # input borders
                    ("#171b26", "#fafafa"),   # alt row
                    ("#525669", "#9598a1"),   # dim text
                    ("#636674", "#787b86"),   # muted text
                    ("#787b86", "#6a6d78"),   # secondary text
                    ("#9598a1", "#555"),      # default text
                    ("#d1d4dc", "#131722"),   # primary text
                ]
                for dark, light in replacements:
                    style = style.replace(dark, light)
            self.setStyleSheet(style)

    @staticmethod
    def _resample_candles(df: pd.DataFrame, target_tf: str) -> pd.DataFrame | None:
        """Resample candles to a different timeframe."""
        import pandas as pd

        freq_map = {
            "1m": "1min", "5m": "5min", "15m": "15min",
            "30m": "30min", "1h": "1h", "2h": "2h",
            "4h": "4h", "1d": "1D", "1w": "1W",
        }
        freq = freq_map.get(target_tf)
        if not freq:
            return None

        try:
            resampled = df.resample(freq).agg({
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
            }).dropna(subset=["open"])
            return resampled
        except Exception:
            return None


def main():
    """Application entry point."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    # Check for Lightweight Charts JS
    js_dir = Path(__file__).parent / "chart" / "js"
    lw_charts = js_dir / "lightweight-charts.standalone.production.js"
    if not lw_charts.exists():
        print("=" * 60)
        print("Lightweight Charts JS not found!")
        print(f"Download from: https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js")
        print(f"Save to: {lw_charts}")
        print("=" * 60)
        print()
        print("Run this command:")
        print(f'  curl -L -o "{lw_charts}" "https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"')
        print()

        # Try to download automatically
        try:
            import urllib.request
            print("Attempting automatic download...")
            url = "https://unpkg.com/lightweight-charts/dist/lightweight-charts.standalone.production.js"
            urllib.request.urlretrieve(url, str(lw_charts))
            print("Downloaded successfully!")
        except Exception as e:
            print(f"Auto-download failed: {e}")
            print("Please download manually and restart.")
            sys.exit(1)

    # WebEngine must be imported before QApplication is created
    from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401

    app = QApplication(sys.argv)
    app.setApplicationName("Chart Analyzer")
    app.setOrganizationName("TraderBot")

    # Load TV credentials from env (enables 15S simulation precision)
    import os
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / "traderbot" / "passes_tv.env")
    load_dotenv(PROJECT_ROOT / "traderbot" / ".env")

    config = AppConfig(
        tv_username=os.getenv("TV_USERNAME", ""),
        tv_password=os.getenv("TV_PASSWORD", ""),
        tv_auth_token=os.getenv("TV_AUTH_TOKEN", ""),
    )

    if config.tv_auth_token:
        print("TradingView Premium: auth_token provided (15S simulation enabled)")
    elif config.tv_username:
        print(f"TradingView Premium: {config.tv_username} (15S simulation enabled)")
    else:
        print("TradingView: anonymous mode (1m simulation precision)")
        print("Set TV_AUTH_TOKEN or TV_USERNAME+TV_PASSWORD in passes_tv.env for 15S precision")

    window = MainWindow(config)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
