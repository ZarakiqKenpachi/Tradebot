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
from traderbot.data.feed import DataFeed
from traderbot.chart.data.tbank_provider import TBankCandleProvider

logger = logging.getLogger(__name__)

# Paths
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STYLE_PATH = Path(__file__).parent / "resources" / "style.qss"
DB_PATH = PROJECT_ROOT / "traderbot" / "data" / "traderbot.db"


class MainWindow(QMainWindow):
    """Main application window."""

    def __init__(self, config: AppConfig | None = None, tbank_feed: DataFeed | None = None):
        super().__init__()
        self._config = config or AppConfig()
        self._tbank_feed = tbank_feed  # T-Bank data feed for simulation
        self._ticker_figi_map = self._load_ticker_figi_map()  # symbol → figi
        self._sim_params = self._load_sim_params()  # risk/trading params from config.yaml
        self._is_dark = True

        self.setWindowTitle("Chart Analyzer — TraderBot")
        self.setMinimumSize(1000, 600)
        self.resize(self._config.window_width, self._config.window_height)

        # ── Services ─────────────────────────────────────
        self._provider = TBankCandleProvider(tbank_feed, self._ticker_figi_map)
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
        self._pb_playing = False
        self._pb_result = None
        self._pb_sim_days = 0
        self._pb_strategy_name = ""
        self._pb_all_trades: list = []  # all trades from simulation
        self._pb_candles: list[dict] = []

        # ── Connect signals ────────────────────���─────────
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
        self._chart._bridge.playback_progress.connect(self._on_playback_progress)
        self._chart._bridge.playback_done.connect(self._on_playback_done)

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

        self._chart.load_symbol(symbol, "MOEX", tf)
        self._statusbar.set_connected(self._provider.is_connected())
        self._load_trades_for_ticker(symbol)

    def _on_symbol_search(self, query: str) -> None:
        """Open symbol search dialog or quick-load known symbol."""
        query = query.strip().upper()
        if not query:
            return

        # Quick load if exact match in known tickers
        if query in self._provider._figi_map:
            self._load_symbol(query, "MOEX")
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
        # Stop playback if running
        if self._pb_playing:
            self._chart._bridge.stop_playback()
            self._pb_playing = False
            self._strategy_bar.set_playback_state(False)

        tf = self._chart.current_timeframe or self._config.default_timeframe
        self._toolbar.set_symbol_text(symbol)
        self._chart.load_symbol(symbol, exchange, tf)
        self._chart._bridge.set_trade_markers(json.dumps([]))
        self._chart._bridge.clear_price_lines()
        self._trades_panel.set_trades([])
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
        dialog.scroll_to_trade.connect(self._on_scroll_to_trade)
        dialog.exec()

    def _on_trade_selected(self, trade: dict) -> None:
        """Highlight trade on chart when row clicked in table."""
        self._chart.highlight_trade(trade)

    def _on_trade_double_clicked(self, trade: dict) -> None:
        """Show trade detail popup on double-click."""
        dialog = TradeDetailDialog(trade, self)
        dialog.scroll_to_trade.connect(self._on_scroll_to_trade)
        dialog.exec()

    def _on_scroll_to_trade(self, trade: dict) -> None:
        """Right-click → Show on chart: scroll + highlight SL/TP lines."""
        self._chart.highlight_trade(trade)

    def _on_strategy_run(self, strategy_name: str) -> None:
        """Run strategy — delegates to playback for visual replay."""
        self._on_playback_start(strategy_name)

    # ── Playback (candle-by-candle replay) ─────────────

    def _on_playback_start(self, strategy_name: str) -> None:
        """Run simulation, then send all data to JS for candle-by-candle replay."""
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
        trades = sorted(result.trades, key=lambda t: t.entry_time)
        self._pb_all_trades = trades
        self._pb_strategy_name = strategy_name
        self._pb_result = result
        self._pb_sim_days = sim_days
        self._pb_playing = True

        # Calculate warmup: end BEFORE the first trade
        warmup = min(60, len(self._pb_candles) // 4)
        if trades:
            first_entry_ts = self._trade_time_to_ts(trades[0].entry_time)
            if first_entry_ts is not None:
                for i, c in enumerate(self._pb_candles):
                    if c["time"] >= first_entry_ts:
                        warmup = max(1, i - 5)
                        break
        warmup = max(1, warmup)

        # Build marker list for JS (entry + exit for each trade)
        markers = []
        for t in trades:
            entry_ts = self._trade_time_to_ts(t.entry_time)
            exit_ts = self._trade_time_to_ts(t.exit_time)
            markers.append({
                "time": entry_ts, "type": "entry",
                "direction": t.direction, "price": t.entry_price,
                "stop_price": t.stop_price, "target_price": t.target_price,
                "entry_reason": t.entry_reason, "qty": t.qty,
                "trade_id": t.id, "ticker": t.ticker,
            })
            markers.append({
                "time": exit_ts, "type": "exit",
                "direction": t.direction, "price": t.exit_price,
                "pnl": t.pnl, "exit_reason": t.exit_reason,
                "entry_reason": t.entry_reason,
                "candles_held": t.candles_held,
                "trade_id": t.id, "ticker": t.ticker,
            })

        # Build trades list for JS P&L tracking
        trades_for_js = []
        for t in trades:
            exit_ts = self._trade_time_to_ts(t.exit_time)
            trades_for_js.append({"_exitTime": exit_ts, "pnl": t.pnl})

        self._trades_panel.set_trades([])

        logger.info(
            "[PLAYBACK] %s: %d candles, %d trades, warmup=%d",
            strategy_name, len(self._pb_candles), len(trades), warmup,
        )

        # Send everything to JS — ONE IPC call starts the entire animation
        speed_ms = self._strategy_bar.get_playback_speed()
        self._chart._bridge.start_playback(
            json.dumps(self._pb_candles),
            json.dumps(markers),
            json.dumps(trades_for_js),
            warmup,
            speed_ms,
        )
        self._strategy_bar.set_playback_state(True)
        self._statusbar.set_status(
            f"Playback: {strategy_name} | {len(trades)} trades to replay"
        )

    @staticmethod
    def _calc_n_bars(tf: str, sim_days: int) -> int:
        """Calculate how many bars to request from TV for a given TF and period.

        Uses 24h/day (not MOEX 16h) to guarantee full coverage.
        Adds warmup (200 bars) and 20% padding.
        """
        bars_per_day = {
            "1m": 1440, "3m": 480, "5m": 288, "15m": 96, "30m": 48,
            "1h": 24, "2h": 12, "4h": 6, "1d": 1,
        }
        bpd = bars_per_day.get(tf, 48)
        needed = int(bpd * sim_days)
        return max(needed + 200, int(needed * 1.3))

    def _run_simulation_for_playback(self, strategy_name: str, df, sim_days: int):
        """Run simulation and return (result, candles_dict, primary_tf).

        Data source priority:
        1. T-Bank 1m data (resampled) — accurate MOEX exchange data, unlimited history
        2. TradingView fallback — if T-Bank unavailable for this ticker
        """
        import pandas as pd
        from datetime import timedelta
        from traderbot.chart.strategy.runner import SimulationConfig

        strategy_cls = self._strategy_runner._registry.get(strategy_name)
        if not strategy_cls:
            QMessageBox.warning(self, "Error", f"Strategy '{strategy_name}' not found")
            return None, None, None

        required = getattr(strategy_cls, "required_timeframes", [])
        candles: dict = {}
        scan_df = None
        scan_tf = "1m"
        lot_size = 10
        price_step = 0.01

        symbol = self._chart.current_symbol
        figi = self._ticker_figi_map.get(symbol)
        if not figi:
            # Try to get FIGI from provider (it loads all MOEX shares)
            figi = self._provider._figi_map.get(symbol)

        if not figi:
            QMessageBox.warning(self, "Error", f"No FIGI found for {symbol}")
            return None, None, None

        self._statusbar.set_status(f"Loading T-Bank data for {symbol} ({sim_days}d)...")
        try:
            all_tfs = list(set(required) | {"1m", "30m"})
            tbank_data = self._tbank_feed.get_candles_history(
                figi, all_tfs, days=sim_days + 15,  # +15 for strategy warmup window
            )
            if tbank_data:
                for tf in required:
                    if tf in tbank_data and not tbank_data[tf].empty:
                        candles[tf] = tbank_data[tf]
                        logger.info(
                            "[PLAYBACK] %s: %d bars, %s .. %s",
                            tf, len(candles[tf]),
                            candles[tf].index[0], candles[tf].index[-1],
                        )
                if "1m" in tbank_data and not tbank_data["1m"].empty:
                    scan_df = tbank_data["1m"]
                    scan_tf = "1m"
                    logger.info("[PLAYBACK] scan 1m: %d bars", len(scan_df))
        except Exception:
            logger.exception("[PLAYBACK] Data load failed for %s", symbol)

        # Get lot_size and price_step from T-Bank
        try:
            lot_size, price_step = self._tbank_feed.broker.get_instrument_info(figi)
            logger.info("[PLAYBACK] %s: lot_size=%d, price_step=%.4f", symbol, lot_size, price_step)
        except Exception:
            logger.warning("[PLAYBACK] Could not get instrument info, using defaults")

        if not candles:
            QMessageBox.warning(self, "Error", "Could not load required timeframes")
            return None, None, None

        cutoff = pd.Timestamp.now(tz="UTC") - timedelta(days=sim_days)
        for tf_key in list(candles.keys()):
            candles[tf_key] = candles[tf_key].loc[candles[tf_key].index >= cutoff]

        if scan_df is not None:
            scan_df = scan_df.loc[scan_df.index >= cutoff]
            if scan_df.empty:
                scan_df = None

        # Validate: primary data should reach close to now
        primary_tf = required[0] if required else list(candles.keys())[0]
        primary_df = candles[primary_tf]
        if not primary_df.empty:
            last_bar = primary_df.index[-1]
            now_utc = pd.Timestamp.now(tz="UTC")
            gap_hours = (now_utc - last_bar).total_seconds() / 3600
            if gap_hours > 48:
                logger.warning(
                    "[PLAYBACK] Data gap: last %s bar is %s (%.0fh ago)",
                    primary_tf, last_bar, gap_hours,
                )

        self._statusbar.set_status(f"Simulating '{strategy_name}'...")
        sim_config = SimulationConfig(
            scan_tf=scan_tf,
            lot_size=lot_size,
            price_step=price_step,
            **self._sim_params,
        )
        result = self._strategy_runner.run(
            strategy_name, candles,
            scan_df=scan_df,
            config=sim_config,
            ticker=symbol,
        )

        primary_df = candles[primary_tf]
        logger.info(
            "[PLAYBACK] Data range: %s .. %s (%d bars, tf=%s, source=%s)",
            primary_df.index[0], primary_df.index[-1],
            len(primary_df), primary_tf,
            "T-Bank" if figi and self._tbank_feed else "TradingView",
        )
        return result, candles, primary_tf

    def _on_playback_progress(self, data: dict) -> None:
        """JS reports playback progress — update status bar and trade table."""
        idx = data.get("idx", 0)
        total = data.get("total", 1)
        shown_trades = data.get("shownTrades", 0)
        pnl = data.get("pnl", 0.0)

        pct = int(idx / total * 100) if total else 0
        pnl_sign = "+" if pnl >= 0 else ""
        self._statusbar.set_status(
            f"Playback {pct}% | {idx}/{total} | "
            f"trades: {shown_trades} | P&L: {pnl_sign}{pnl:.2f}"
        )

        # Update trades table when a trade event occurred
        if data.get("tradeEvent") and self._pb_all_trades:
            self._trades_panel.set_trades(self._pb_all_trades[:shown_trades])

    def _on_playback_done(self, data: dict) -> None:
        """JS playback finished — show final results and save to journal."""
        self._pb_playing = False
        self._strategy_bar.set_playback_state(False)

        # Show all trades in table
        result = self._pb_result
        if result and result.trades:
            self._trades_panel.set_trades(result.trades)

        # Show all candles + all markers (static view after playback)
        if self._pb_candles:
            self._chart._bridge.set_candles(json.dumps(self._pb_candles))
            if result and result.trades:
                all_markers = []
                for t in result.trades:
                    entry_ts = self._trade_time_to_ts(t.entry_time)
                    exit_ts = self._trade_time_to_ts(t.exit_time)
                    all_markers.append({
                        "time": entry_ts, "type": "entry",
                        "direction": t.direction, "price": t.entry_price,
                        "stop_price": t.stop_price, "target_price": t.target_price,
                        "entry_reason": t.entry_reason, "qty": t.qty,
                        "trade_id": t.id, "ticker": t.ticker,
                    })
                    all_markers.append({
                        "time": exit_ts, "type": "exit",
                        "direction": t.direction, "price": t.exit_price,
                        "pnl": t.pnl, "exit_reason": t.exit_reason,
                        "entry_reason": t.entry_reason,
                        "candles_held": t.candles_held,
                        "trade_id": t.id, "ticker": t.ticker,
                    })
                self._chart._bridge.set_trade_markers(json.dumps(all_markers))

        # Save to simulation journal
        strategy_name = self._pb_strategy_name
        sim_days = self._pb_sim_days
        if result and strategy_name:
            scan_tf = "1m"
            sim_config = SimulationConfig(scan_tf=scan_tf)
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
            pnl_sign = "+" if result.total_pnl >= 0 else ""
            self._statusbar.set_status(
                f"Run #{run_id} | {strategy_name} {sim_days}d: "
                f"{result.setups_found} setups, {len(result.trades)} trades | "
                f"P&L: {pnl_sign}{result.total_pnl:.2f} | "
                f"DD: {result.max_drawdown*100:.1f}% | "
                f"Balance: {result.final_balance:.0f}"
            )

    def _on_playback_pause(self) -> None:
        """Toggle pause/resume — delegate to JS."""
        self._chart._bridge.toggle_playback()

    def _on_playback_step(self) -> None:
        """Advance one candle while paused — delegate to JS."""
        self._chart._bridge.step_playback()

    def _on_playback_stop(self) -> None:
        """Stop playback — delegate to JS, then show final results."""
        self._chart._bridge.stop_playback()
        self._on_playback_done({})

    def _on_playback_speed_changed(self, _index: int) -> None:
        """Update playback speed — delegate to JS."""
        if self._pb_playing:
            speed_ms = self._strategy_bar.get_playback_speed()
            self._chart._bridge.set_playback_speed(speed_ms)

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
        """Fill quick ticker combo with config tickers + all MOEX shares from T-Bank."""
        tickers: list[tuple[str, str, str]] = []
        seen = set()

        # 1. Tickers from config.yaml (trading tickers first)
        for symbol in self._ticker_figi_map:
            if symbol not in seen:
                tickers.append((symbol, "MOEX", ""))
                seen.add(symbol)

        # 2. All MOEX shares from T-Bank API
        all_shares = self._provider.get_all_symbols()
        for info in all_shares:
            if info.symbol not in seen:
                tickers.append((info.symbol, info.exchange, info.description))
                seen.add(info.symbol)

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

    @staticmethod
    def _load_ticker_figi_map() -> dict[str, str]:
        """Load symbol→FIGI mapping from config.yaml."""
        config_path = PROJECT_ROOT / "traderbot" / "config.yaml"
        if not config_path.exists():
            return {}
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            return {
                name: tc.get("figi", "")
                for name, tc in cfg.get("tickers", {}).items()
                if tc.get("figi")
            }
        except Exception:
            return {}

    @staticmethod
    def _load_sim_params() -> dict:
        """Load simulation parameters from config.yaml (risk + trading + backtest)."""
        config_path = PROJECT_ROOT / "traderbot" / "config.yaml"
        if not config_path.exists():
            return {}
        try:
            import yaml
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = yaml.safe_load(f)
            risk = cfg.get("risk", {})
            trading = cfg.get("trading", {})
            bt = cfg.get("backtest", {})
            return {
                "initial_balance": bt.get("initial_balance", 100_000.0),
                "risk_pct": risk.get("risk_pct", 0.10),
                "max_position_pct": risk.get("max_position_pct", 1.50),
                "commission_pct": trading.get("commission_pct", 0.0004),
                "slippage_pct": bt.get("slippage_pct", 0.0005),
                "max_candles_timeout": trading.get("max_candles_timeout", 12),
                "max_consecutive_sl": risk.get("max_consecutive_sl", 3),
            }
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

    config = AppConfig()

    # Initialize T-Bank data feed (all data: chart display + simulation)
    # Read-only token for market data — safe to commit, cannot trade or access accounts
    TBANK_MARKET_DATA_TOKEN = "t.2ioHpwYVj4t12B_iRoqmfE3Rb4jkLIN1cSB7RDzBoAEJUjPo4tdkcjpiW_NGuzDKoj5aKVLbSc-syHejhbgIpg"
    try:
        from traderbot.broker.tbank import TBankBroker
        broker = TBankBroker(token=TBANK_MARKET_DATA_TOKEN, sandbox=True, app_name="ChartAnalyzer-MD")
        tbank_feed = DataFeed(broker)
        print("T-Bank: connected (real MOEX data)")
    except Exception as e:
        print(f"T-Bank: connection failed ({e})")
        sys.exit(1)

    window = MainWindow(config, tbank_feed=tbank_feed)
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
