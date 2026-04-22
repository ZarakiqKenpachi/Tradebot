"""Chart Analyzer — Desktop application entry point.

Usage:
    py -3.12 -m traderbot.chart.app
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer
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
from traderbot.chart.widgets.toolbar import MainToolbar
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

        # ── Toolbar ──────────────────────────────────────
        self._toolbar = MainToolbar(self)
        self._toolbar.set_strategies(
            self._strategy_runner.get_strategy_names(),
            strategy_tickers=self._load_strategy_tickers(),
        )
        self._toolbar.set_search_service(self._search_service)
        self._populate_ticker_list()
        self.addToolBar(self._toolbar)

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
        self._toolbar.strategy_run_requested.connect(self._on_strategy_run)
        self._toolbar.fit_requested.connect(self._chart.fit_content)
        self._toolbar.crosshair_mode_changed.connect(self._chart.set_crosshair_mode)
        self._toolbar.drawing_mode_toggled.connect(self._chart.toggle_drawing_mode)
        self._toolbar.clear_lines_requested.connect(self._chart.clear_user_lines)

        # Chart
        self._chart.status_changed.connect(self._statusbar.set_status)
        self._chart.crosshair_data.connect(self._statusbar.set_ohlcv)
        self._chart.trade_marker_clicked.connect(self._on_marker_clicked)
        self._chart.candles_loaded.connect(self._on_candles_loaded)

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
        """After candles load, compute and show EMA."""
        self._chart.set_ema(20, 50, visible=True)

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

        sim_days = self._toolbar.get_selected_days()
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
                # Simple replacements for light theme
                style = style.replace("#131722", "#ffffff")
                style = style.replace("#1e222d", "#f0f3fa")
                style = style.replace("#2a2e39", "#d6dcde")
                style = style.replace("#363a45", "#c8c8c8")
                style = style.replace("#d1d4dc", "#131722")
                style = style.replace("#787b86", "#6a6d78")
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
