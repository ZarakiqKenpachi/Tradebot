"""Strategy simulation engine — mirrors backtest/engine.py logic.

Uses T-Bank 1m candles for SL/TP/fill scanning (same data source as backtest).
- Limit order fill simulation with invalidation
- Slippage on market orders (SL, timeout)
- RiskManager with balance tracking
- Consecutive SL blocking per ticker per day
- Position timeout (max_candles_timeout)
- Margin overnight cost (T-Bank tariffs)
- Price step rounding
- Sliding window (15 days) for strategy data
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from traderbot.chart.data.provider import CandleProvider
from traderbot.chart.trades.models import TradeDisplayRecord

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
_DAYS_WINDOW = 15          # sliding data window for strategies (same as live)
_MIN_WARMUP = 20           # min bars before first signal (matches backtest _MIN_BARS)
_PENDING_TIMEOUT = 20      # 30m-candle equivalents for limit order timeout
_HIGH_PRECISION_DAYS = 7   # use 1m iteration for sim periods <= this

# Timeframe durations for completion checking and live-resampling
_TF_DURATION = {
    "1m": timedelta(minutes=1),
    "5m": timedelta(minutes=5),
    "15m": timedelta(minutes=15),
    "30m": timedelta(minutes=30),
    "1h": timedelta(hours=1),
    "2h": timedelta(hours=2),
    "4h": timedelta(hours=4),
    "1d": timedelta(days=1),
}

_FREQ_MAP = {
    "5m": "5min", "15m": "15min", "30m": "30min",
    "1h": "1h", "2h": "2h", "4h": "4h", "1d": "1D",
}


def _build_strategy_window(
    candles: dict[str, pd.DataFrame],
    current_time: pd.Timestamp,
    primary_tf: str,
) -> dict[str, pd.DataFrame]:
    """Build sliding window for strategy (standard mode).

    Include all completed bars up to current_time, exclude 1m.
    """
    window_start = current_time - timedelta(days=_DAYS_WINDOW)
    return {
        tf: df[(df.index > window_start) & (df.index <= current_time)]
        for tf, df in candles.items()
        if tf != "1m"
    }


class _ResampleCache:
    """Incremental live-resampler: pre-computes completed bars,
    builds partial current bar from 1m data — replicates feed.get_candles().

    In live, DataFeed loads 1m candles and resamples to 30m/1h/etc.
    The current (incomplete) bar is visible with partial data.
    This cache replicates that behavior efficiently.
    """

    def __init__(self, candles_1m: pd.DataFrame, timeframes: list[str]):
        self._df = candles_1m
        self._tfs = [tf for tf in timeframes if tf != "1m" and tf in _FREQ_MAP]
        # Pre-resample full dataset once
        self._full: dict[str, pd.DataFrame] = {}
        for tf in self._tfs:
            self._full[tf] = candles_1m.resample(_FREQ_MAP[tf]).agg({
                "open": "first", "high": "max", "low": "min",
                "close": "last", "volume": "sum",
            }).dropna(subset=["open"])

    def get_window(self, current_time: pd.Timestamp) -> dict[str, pd.DataFrame]:
        """Strategy window at current_time with completed + partial bars."""
        window_start = current_time - timedelta(days=_DAYS_WINDOW)
        result = {}
        for tf in self._tfs:
            dur = _TF_DURATION[tf]
            full_df = self._full[tf]

            # Completed bars: bar_start + dur <= current_time
            cutoff = current_time - dur
            completed = full_df[
                (full_df.index > window_start) & (full_df.index <= cutoff)
            ]

            # Partial (current) bar: 1m data from bar_start to current_time
            partial_start = cutoff + dur  # = current_time (first incomplete bar start)
            # Align to TF boundary
            if not completed.empty:
                partial_start = completed.index[-1] + dur

            partial_1m = self._df[
                (self._df.index >= partial_start) & (self._df.index <= current_time)
            ]
            if not partial_1m.empty:
                pbar = pd.DataFrame({
                    "open": [float(partial_1m.iloc[0]["open"])],
                    "high": [float(partial_1m["high"].max())],
                    "low": [float(partial_1m["low"].min())],
                    "close": [float(partial_1m.iloc[-1]["close"])],
                    "volume": [float(partial_1m["volume"].sum())],
                }, index=[partial_start])
                result[tf] = pd.concat([completed, pbar])
            else:
                result[tf] = completed
        return result


@dataclass
class SimulationConfig:
    """Configuration for the simulation engine."""
    initial_balance: float = 100_000.0
    risk_pct: float = 0.10
    max_position_pct: float = 1.50
    commission_pct: float = 0.0004
    slippage_pct: float = 0.0005
    max_candles_timeout: int = 12       # in 30m candles (from config.yaml trading section)
    max_consecutive_sl: int = 3
    lot_size: int = 1                   # overridden per ticker from T-Bank API
    price_step: float = 0.01            # overridden per ticker from T-Bank API
    max_open_positions: int = 4          # global limit across all tickers
    scan_tf: str = "1m"                 # granularity for SL/TP/fill scanning
    dividend_dates: list = field(default_factory=list)  # list of last_buy_date (date objects)


@dataclass
class _VirtualPosition:
    ticker: str
    direction: str          # "BUY" or "SELL"
    entry_price: float
    stop_price: float
    target_price: float
    qty: int                # in lots
    lot_size: int
    entry_time: datetime
    entry_reason: str
    balance_at_entry: float = 0.0
    candles_held: int = 0
    last_30m_time: pd.Timestamp | None = None


@dataclass
class StrategyRunResult:
    """Result of running a strategy on historical data."""
    strategy_name: str
    trades: list[TradeDisplayRecord] = field(default_factory=list)
    setups_found: int = 0
    total_pnl: float = 0.0
    final_balance: float = 0.0
    max_drawdown: float = 0.0
    errors: list[str] = field(default_factory=list)


class StrategyRunner:
    """Run strategies with full simulation matching live/backtest behavior.

    Data flow:
    1. Load strategy candles (30m, 1h) + 1m scan data from T-Bank
    2. Walk bar-by-bar on the primary strategy timeframe
    3. Inside each bar, scan 1m candles for precise SL/TP/fill
    """

    def __init__(self, provider: CandleProvider | None = None):
        self._provider = provider
        self._registry: dict[str, type] = {}
        self._load_registry()

    def _load_registry(self) -> None:
        try:
            from traderbot.strategies.registry import STRATEGY_REGISTRY
            self._registry = STRATEGY_REGISTRY
            logger.info("[RUNNER] Loaded %d strategies", len(self._registry))
        except ImportError:
            logger.warning("[RUNNER] Could not import strategy registry")

    def get_strategy_names(self) -> list[str]:
        return list(self._registry.keys())

    def run(
        self,
        strategy_name: str,
        candles: dict[str, pd.DataFrame],
        scan_df: pd.DataFrame | None = None,
        config: SimulationConfig | None = None,
        ticker: str = "",
    ) -> StrategyRunResult:
        """Run full simulation.

        Args:
            strategy_name: Name from STRATEGY_REGISTRY
            candles: {timeframe: DataFrame} — strategy timeframes (30m, 1h, etc.)
            scan_df: High-resolution candles (15S) for SL/TP scanning.
                     If None, falls back to primary TF (less precise).
            config: Simulation parameters
            ticker: Ticker name for display

        Returns:
            StrategyRunResult with trades, P&L, drawdown.
        """
        cfg = config or SimulationConfig()
        result = StrategyRunResult(strategy_name=strategy_name)

        if strategy_name not in self._registry:
            result.errors.append(f"Strategy '{strategy_name}' not found")
            return result

        try:
            strategy = self._registry[strategy_name]()
        except Exception as e:
            result.errors.append(f"Failed to create strategy: {e}")
            return result

        required_tfs = getattr(strategy, "required_timeframes", [])
        for tf in required_tfs:
            if tf not in candles or candles[tf].empty:
                result.errors.append(f"Missing required timeframe: {tf}")
                return result

        # Primary TF for bar-by-bar iteration
        primary_tf = required_tfs[0] if required_tfs else list(candles.keys())[0]
        primary_df = candles[primary_tf]

        if len(primary_df) < _MIN_WARMUP:
            result.errors.append(f"Not enough data: {len(primary_df)} bars in {primary_tf}")
            return result

        # Build 30m index for candle counting (same as live)
        df_30m = candles.get("30m")

        # State
        balance = cfg.initial_balance
        peak_balance = balance
        max_dd = 0.0
        position: _VirtualPosition | None = None
        pending = None       # (setup, qty, balance_at_signal)
        pending_30m_count = 0
        pending_last_30m: pd.Timestamp | None = None
        consecutive_sl = 0
        sl_date = ""
        trade_id = 0

        # ── Main loop ────────────────────────────────────
        for i in range(_MIN_WARMUP, len(primary_df)):
            current_time = primary_df.index[i]
            prev_time = primary_df.index[i - 1]

            # Get 15S (or scan_df) slice for this bar: (prev_close, current_close]
            bar_scan = _get_scan_slice(scan_df, prev_time, current_time)

            # Fallback: if no scan data for this bar, synthesize from primary bar OHLC
            if bar_scan.empty:
                bar_scan = _synthesize_scan_bar(primary_df, i, current_time)

            # Current 30m bar for candle counting
            current_30m = _get_30m_at(df_30m, current_time)

            # ── 1. Manage open position ──────────────────
            if position is not None:
                # Count 30m candles held
                if current_30m is not None and current_30m != position.last_30m_time:
                    position.candles_held += 1
                    position.last_30m_time = current_30m

                # Scan for SL/TP on high-resolution data
                exit_trade = _scan_exit(position, bar_scan, cfg)
                if exit_trade is not None:
                    exit_trade.id = trade_id
                    exit_trade.ticker = ticker
                    trade_id += 1
                    result.trades.append(exit_trade)
                    balance += exit_trade.pnl
                    consecutive_sl, sl_date = _update_sl_counter(
                        exit_trade.exit_reason, consecutive_sl, sl_date, exit_trade.exit_time,
                    )
                    position = None
                    # Track drawdown
                    peak_balance = max(peak_balance, balance)
                    dd = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0
                    max_dd = max(max_dd, dd)
                    continue

                # Timeout check
                if position.candles_held >= cfg.max_candles_timeout:
                    exit_price = _get_last_price(bar_scan, primary_df, i)
                    exit_price = _apply_slippage(exit_price, position.direction, cfg.slippage_pct)
                    exit_trade = _close_position(position, exit_price, "timeout", current_time, cfg)
                    exit_trade.id = trade_id
                    exit_trade.ticker = ticker
                    trade_id += 1
                    result.trades.append(exit_trade)
                    balance += exit_trade.pnl
                    consecutive_sl, sl_date = _update_sl_counter(
                        "timeout", consecutive_sl, sl_date, str(current_time),
                    )
                    position = None
                    peak_balance = max(peak_balance, balance)
                    dd = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0
                    max_dd = max(max_dd, dd)

                continue  # position open, skip setup search

            # ── 2. Pending limit order ───────────────────
            if pending is not None:
                if current_30m is not None and current_30m != pending_last_30m:
                    pending_30m_count += 1
                    pending_last_30m = current_30m

                if pending_30m_count >= _PENDING_TIMEOUT:
                    pending = None
                    pending_30m_count = 0
                    pending_last_30m = None
                    # Fall through to setup search
                else:
                    setup, qty, pending_bal = pending
                    fill_result = _scan_fill(setup, bar_scan, cfg)

                    if fill_result == "invalidated":
                        pending = None
                        pending_30m_count = 0
                        pending_last_30m = None
                    elif fill_result is not None:
                        fill_time = fill_result
                        position = _VirtualPosition(
                            ticker=ticker,
                            direction=setup.direction.value,
                            entry_price=setup.entry_price,
                            stop_price=setup.stop_price,
                            target_price=setup.target_price,
                            qty=qty, lot_size=cfg.lot_size,
                            entry_time=fill_time,
                            entry_reason=setup.entry_reason,
                            balance_at_entry=pending_bal,
                            last_30m_time=current_30m,
                        )
                        pending = None
                        pending_30m_count = 0
                        pending_last_30m = None

                        # Check SL/TP on remaining scan candles after fill
                        remaining = _get_scan_slice(scan_df, fill_time, current_time)
                        if remaining.empty:
                            remaining = bar_scan[bar_scan.index > fill_time]
                        if not remaining.empty:
                            exit_trade = _scan_exit(position, remaining, cfg)
                            if exit_trade is not None:
                                exit_trade.id = trade_id
                                exit_trade.ticker = ticker
                                trade_id += 1
                                result.trades.append(exit_trade)
                                balance += exit_trade.pnl
                                consecutive_sl, sl_date = _update_sl_counter(
                                    exit_trade.exit_reason, consecutive_sl, sl_date, exit_trade.exit_time,
                                )
                                position = None
                    else:
                        continue  # still waiting for fill

            # ── 3. Search for new setup ──────────────────
            # Weekend filter
            current_msk = current_time.tz_convert(MSK) if current_time.tzinfo else current_time
            if hasattr(current_msk, 'weekday') and current_msk.weekday() >= 5:
                continue

            # Consecutive SL block
            today_str = str(current_msk.date()) if hasattr(current_msk, 'date') else ""
            if sl_date == today_str and consecutive_sl >= cfg.max_consecutive_sl:
                continue

            # Sliding window for strategy (exclude 1m)
            window = _build_strategy_window(candles, current_time, primary_tf)

            try:
                setup = strategy.find_setup(window)
            except Exception as e:
                result.errors.append(f"Error at bar {i}: {e}")
                continue

            if setup is None:
                continue

            result.setups_found += 1

            # Dividend filter: нельзя шортить перед дивидендной отсечкой
            if setup.direction.value == "SELL" and cfg.dividend_dates:
                bar_date = current_time.date() if hasattr(current_time, 'date') else None
                if bar_date and _is_near_dividend(bar_date, cfg.dividend_dates):
                    logger.debug("[RUNNER] Skipped SELL: near dividend cutoff")
                    continue

            # Round prices to step
            if cfg.price_step > 0:
                setup = _round_setup(setup, cfg.price_step)

            # Limit order price validation:
            # BUY limit cannot be above current price, SELL limit cannot be below
            current_price = _get_current_price(bar_scan, primary_df, i)
            if current_price > 0:
                is_buy = setup.direction.value == "BUY"
                if is_buy and setup.entry_price > current_price:
                    # Check if price drops to entry within this bar (live polls every 60s)
                    if bar_scan.empty or bar_scan["low"].min() > setup.entry_price:
                        logger.debug(
                            "[RUNNER] Skipped BUY: entry %.2f > market %.2f (no intra-bar reach)",
                            setup.entry_price, current_price,
                        )
                        continue
                if not is_buy and setup.entry_price < current_price:
                    # Check if price rises to entry within this bar
                    if bar_scan.empty or bar_scan["high"].max() < setup.entry_price:
                        logger.debug(
                            "[RUNNER] Skipped SELL: entry %.2f < market %.2f (no intra-bar reach)",
                            setup.entry_price, current_price,
                        )
                        continue

            # Position sizing
            qty = _position_size(
                balance, setup.entry_price, setup.stop_price,
                cfg.risk_pct, cfg.max_position_pct, cfg.lot_size,
            )
            if qty < 1:
                continue

            strategy.on_trade_opened()

            # Try to fill on current bar's scan data
            fill_result = _scan_fill(setup, bar_scan, cfg)

            if fill_result == "invalidated":
                pass
            elif fill_result is not None:
                fill_time = fill_result
                position = _VirtualPosition(
                    ticker=ticker,
                    direction=setup.direction.value,
                    entry_price=setup.entry_price,
                    stop_price=setup.stop_price,
                    target_price=setup.target_price,
                    qty=qty, lot_size=cfg.lot_size,
                    entry_time=fill_time,
                    entry_reason=setup.entry_reason,
                    balance_at_entry=balance,
                    last_30m_time=current_30m,
                )
                # Check immediate exit
                remaining = _get_scan_slice(scan_df, fill_time, current_time)
                if remaining.empty:
                    remaining = bar_scan[bar_scan.index > fill_time]
                if not remaining.empty:
                    exit_trade = _scan_exit(position, remaining, cfg)
                    if exit_trade is not None:
                        exit_trade.id = trade_id
                        exit_trade.ticker = ticker
                        trade_id += 1
                        result.trades.append(exit_trade)
                        balance += exit_trade.pnl
                        consecutive_sl, sl_date = _update_sl_counter(
                            exit_trade.exit_reason, consecutive_sl, sl_date, exit_trade.exit_time,
                        )
                        position = None
            else:
                # Place as pending limit order
                pending = (setup, qty, balance)
                pending_30m_count = 0
                pending_last_30m = current_30m

        # ── Close remaining position at last price ───────
        if position is not None:
            last_price = float(primary_df.iloc[-1]["close"])
            last_price = _apply_slippage(last_price, position.direction, cfg.slippage_pct)
            exit_trade = _close_position(
                position, last_price, "end_of_data", primary_df.index[-1], cfg,
            )
            exit_trade.id = trade_id
            exit_trade.ticker = ticker
            result.trades.append(exit_trade)
            balance += exit_trade.pnl

        # ── Summary ──────────────────────────────────────
        result.total_pnl = balance - cfg.initial_balance
        result.final_balance = balance
        result.max_drawdown = max_dd

        peak_balance = max(peak_balance, balance)
        dd = (peak_balance - balance) / peak_balance if peak_balance > 0 else 0
        result.max_drawdown = max(max_dd, dd)

        logger.info(
            "[RUNNER] %s on %s: %d setups, %d trades, P&L=%.2f, DD=%.1f%%",
            strategy_name, ticker, result.setups_found, len(result.trades),
            result.total_pnl, result.max_drawdown * 100,
        )
        return result

    # ── Multi-ticker interleaved simulation ──────────────

    def run_interleaved(
        self,
        ticker_data: dict[str, dict],
        config: SimulationConfig,
        sim_start: pd.Timestamp | None = None,
    ) -> tuple[dict[str, list], float]:
        """Run chronological interleaved simulation across all tickers.

        Two modes:
        - **High-precision** (sim_days <= _HIGH_PRECISION_DAYS): iterate on 1m
          bars, build strategy window by resampling 1m data (completed bars +
          partial current bar) — replicates live ``feed.get_candles()`` exactly.
        - **Standard**: iterate on primary-TF bars (faster for long backtests).

        Args:
            ticker_data: {symbol: {strategy_name, candles, scan_df, lot_size,
                          price_step, dividend_dates, required}}
            config: SimulationConfig with max_open_positions
            sim_start: Only output trades after this timestamp.

        Returns:
            (trades_by_ticker, final_balance)
        """
        max_open = config.max_open_positions

        # Determine mode
        if sim_start is not None:
            sim_days = (pd.Timestamp.now(tz="UTC") - sim_start).total_seconds() / 86400
        else:
            sim_days = 999
        high_precision = sim_days <= _HIGH_PRECISION_DAYS + 0.5  # margin for runtime

        # Per-ticker state
        states: dict[str, _InterleavedState] = {}
        for symbol, td in ticker_data.items():
            strategy_name = td["strategy_name"]
            if strategy_name not in self._registry:
                continue
            try:
                strategy = self._registry[strategy_name]()
            except Exception:
                continue

            required_tfs = getattr(strategy, "required_timeframes", [])
            primary_tf = required_tfs[0] if required_tfs else list(td["candles"].keys())[0]
            primary_df = td["candles"].get(primary_tf)
            if primary_df is None or len(primary_df) < _MIN_WARMUP:
                continue

            scan_df = td.get("scan_df")

            # Build resample cache for high-precision mode
            rcache = None
            if high_precision and scan_df is not None and not scan_df.empty:
                rcache = _ResampleCache(scan_df, required_tfs)

            states[symbol] = _InterleavedState(
                ticker=symbol,
                strategy=strategy,
                strategy_name=strategy_name,
                candles=td["candles"],
                primary_tf=primary_tf,
                primary_df=primary_df,
                scan_df=scan_df,
                df_30m=td["candles"].get("30m"),
                lot_size=td.get("lot_size", 1),
                price_step=td.get("price_step", 0.01),
                dividend_dates=td.get("dividend_dates", []),
                resample_cache=rcache,
            )

        if not states:
            return {}, config.initial_balance

        # ── Build event list ─────────────────────────────
        # (timestamp, symbol, bar_index, is_1m)
        # Sort by (timestamp, config_order) to replicate live processing:
        # live processes all tickers in config order within each poll cycle.
        events: list[tuple] = []
        config_order = {sym: i for i, sym in enumerate(ticker_data.keys())}

        if high_precision:
            for symbol, st in states.items():
                if st.scan_df is not None and not st.scan_df.empty and st.resample_cache is not None:
                    for j in range(1, len(st.scan_df)):
                        events.append((st.scan_df.index[j], symbol, j, True))
                else:
                    # Fallback to primary TF for tickers without 1m data
                    for i in range(_MIN_WARMUP, len(st.primary_df)):
                        events.append((st.primary_df.index[i], symbol, i, False))
            logger.info("[RUNNER] High-precision mode (1m iteration, %d tickers)", len(states))
        else:
            for symbol, st in states.items():
                for i in range(_MIN_WARMUP, len(st.primary_df)):
                    events.append((st.primary_df.index[i], symbol, i, False))

        events.sort(key=lambda e: (e[0], config_order.get(e[1], 999)))

        balance = config.initial_balance
        trades_by_ticker: dict[str, list] = {s: [] for s in states}
        trade_id = 0
        sim_started = sim_start is None  # True if no warmup needed

        for ts, symbol, idx, is_1m in events:
            # ── Reset at sim_start boundary ─────────────────
            # Warmup period builds strategy state naturally (find_setup +
            # on_trade_opened calls).  At sim_start we reset balance,
            # positions, and counters so warmup trades don't bleed into
            # the comparison period.  Strategy _pending_* state is KEPT —
            # it was built by warmup and represents the same state the
            # live bot would have accumulated from prior trading activity.
            if not sim_started and ts >= sim_start:
                sim_started = True
                balance = config.initial_balance
                for s in states.values():
                    s.position = None
                    s.pending = None
                    s.pending_30m_count = 0
                    s.pending_last_30m = None
                    s.consecutive_sl = 0
                    s.sl_date = ""
                trades_by_ticker = {s: [] for s in states}
                trade_id = 0
            st = states[symbol]
            cfg = SimulationConfig(
                initial_balance=balance,
                risk_pct=config.risk_pct,
                max_position_pct=config.max_position_pct,
                commission_pct=config.commission_pct,
                slippage_pct=config.slippage_pct,
                max_candles_timeout=config.max_candles_timeout,
                max_consecutive_sl=config.max_consecutive_sl,
                lot_size=st.lot_size,
                price_step=st.price_step,
                dividend_dates=st.dividend_dates,
            )

            # ── Resolve current bar data ────────────────
            if is_1m:
                current_time = st.scan_df.index[idx]
                prev_time = st.scan_df.index[idx - 1]
                bar_scan = st.scan_df.iloc[[idx]]  # single 1m bar
            else:
                current_time = st.primary_df.index[idx]
                prev_time = st.primary_df.index[idx - 1]
                bar_scan = _get_scan_slice(st.scan_df, prev_time, current_time)
                if bar_scan.empty:
                    bar_scan = _synthesize_scan_bar(st.primary_df, idx, current_time)

            current_30m = _get_30m_at(st.df_30m, current_time)

            # ── 1. Manage open position ──────────────────
            if st.position is not None:
                if current_30m is not None and current_30m != st.position.last_30m_time:
                    st.position.candles_held += 1
                    st.position.last_30m_time = current_30m

                exit_trade = _scan_exit(st.position, bar_scan, cfg)
                if exit_trade is not None:
                    exit_trade.id = trade_id
                    exit_trade.ticker = symbol
                    trade_id += 1
                    trades_by_ticker[symbol].append(exit_trade)
                    balance += exit_trade.pnl
                    st.consecutive_sl, st.sl_date = _update_sl_counter(
                        exit_trade.exit_reason, st.consecutive_sl, st.sl_date, exit_trade.exit_time,
                    )
                    st.position = None
                    continue

                if st.position.candles_held >= cfg.max_candles_timeout:
                    exit_price = float(bar_scan.iloc[-1]["close"]) if not bar_scan.empty else float(st.primary_df.iloc[-1]["close"])
                    exit_price = _apply_slippage(exit_price, st.position.direction, cfg.slippage_pct)
                    exit_trade = _close_position(st.position, exit_price, "timeout", current_time, cfg)
                    exit_trade.id = trade_id
                    exit_trade.ticker = symbol
                    trade_id += 1
                    trades_by_ticker[symbol].append(exit_trade)
                    balance += exit_trade.pnl
                    st.consecutive_sl, st.sl_date = _update_sl_counter(
                        "timeout", st.consecutive_sl, st.sl_date, str(current_time),
                    )
                    st.position = None

                continue

            # ── 2. Pending limit order ───────────────────
            if st.pending is not None:
                if current_30m is not None and current_30m != st.pending_last_30m:
                    st.pending_30m_count += 1
                    st.pending_last_30m = current_30m

                if st.pending_30m_count >= _PENDING_TIMEOUT:
                    st.pending = None
                    st.pending_30m_count = 0
                    st.pending_last_30m = None
                else:
                    setup, qty, pending_bal = st.pending
                    fill_result = _scan_fill(setup, bar_scan, cfg)

                    if fill_result == "invalidated":
                        st.pending = None
                        st.pending_30m_count = 0
                        st.pending_last_30m = None
                    elif fill_result is not None:
                        fill_time = fill_result
                        st.position = _VirtualPosition(
                            ticker=symbol,
                            direction=setup.direction.value,
                            entry_price=setup.entry_price,
                            stop_price=setup.stop_price,
                            target_price=setup.target_price,
                            qty=qty, lot_size=cfg.lot_size,
                            entry_time=fill_time,
                            entry_reason=setup.entry_reason,
                            balance_at_entry=pending_bal,
                            last_30m_time=current_30m,
                        )
                        st.pending = None
                        st.pending_30m_count = 0
                        st.pending_last_30m = None

                        remaining = _get_scan_slice(st.scan_df, fill_time, current_time)
                        if remaining.empty:
                            remaining = bar_scan[bar_scan.index > fill_time]
                        if not remaining.empty:
                            exit_trade = _scan_exit(st.position, remaining, cfg)
                            if exit_trade is not None:
                                exit_trade.id = trade_id
                                exit_trade.ticker = symbol
                                trade_id += 1
                                trades_by_ticker[symbol].append(exit_trade)
                                balance += exit_trade.pnl
                                st.consecutive_sl, st.sl_date = _update_sl_counter(
                                    exit_trade.exit_reason, st.consecutive_sl,
                                    st.sl_date, exit_trade.exit_time,
                                )
                                st.position = None
                    else:
                        continue  # still waiting for fill

            # ── 3. Search for new setup ──────────────────
            current_msk = current_time.tz_convert(MSK) if current_time.tzinfo else current_time
            if hasattr(current_msk, 'weekday') and current_msk.weekday() >= 5:
                continue

            today_str = str(current_msk.date()) if hasattr(current_msk, 'date') else ""
            if st.sl_date == today_str and st.consecutive_sl >= cfg.max_consecutive_sl:
                continue

            # Global open count: positions + pending across ALL tickers
            open_count = sum(
                1 for s in states.values()
                if s.position is not None or s.pending is not None
            )
            if open_count >= max_open:
                continue

            # Strategy window
            if is_1m and st.resample_cache is not None:
                window = st.resample_cache.get_window(current_time)
                # For higher TFs (>=4h), use original broker data instead of
                # 1m resampling — exchange session boundaries make 1m→4h/1d
                # resampling inaccurate (different bar opens/closes).
                _BROKER_OVERRIDE_TFS = ("4h", "1d")
                window_start = current_time - timedelta(days=_DAYS_WINDOW)
                for tf in _BROKER_OVERRIDE_TFS:
                    if tf in st.candles and not st.candles[tf].empty:
                        tf_df = st.candles[tf]
                        window[tf] = tf_df[
                            (tf_df.index > window_start) & (tf_df.index <= current_time)
                        ]
            else:
                window = _build_strategy_window(st.candles, current_time, st.primary_tf)

            try:
                setup = st.strategy.find_setup(window)
            except Exception:
                continue

            if setup is None:
                continue

            if setup.direction.value == "SELL" and cfg.dividend_dates:
                bar_date = current_time.date() if hasattr(current_time, 'date') else None
                if bar_date and _is_near_dividend(bar_date, cfg.dividend_dates):
                    continue

            if cfg.price_step > 0:
                setup = _round_setup(setup, cfg.price_step)

            # Price validation: use current 1m price (high precision)
            # or intra-bar range (standard)
            if not bar_scan.empty:
                current_price = float(bar_scan.iloc[0]["open"])
            else:
                current_price = 0.0

            if current_price > 0:
                is_buy = setup.direction.value == "BUY"
                if is_buy and setup.entry_price > current_price:
                    if is_1m:
                        continue  # Live: BUY limit > market → rejected
                    if bar_scan.empty or bar_scan["low"].min() > setup.entry_price:
                        continue
                if not is_buy and setup.entry_price < current_price:
                    if is_1m:
                        continue  # Live: SELL limit < market → rejected
                    if bar_scan.empty or bar_scan["high"].max() < setup.entry_price:
                        continue

            qty = _position_size(
                balance, setup.entry_price, setup.stop_price,
                cfg.risk_pct, cfg.max_position_pct, cfg.lot_size,
            )
            if qty < 1:
                continue

            st.strategy.on_trade_opened()

            fill_result = _scan_fill(setup, bar_scan, cfg)

            if fill_result == "invalidated":
                pass
            elif fill_result is not None:
                fill_time = fill_result
                st.position = _VirtualPosition(
                    ticker=symbol,
                    direction=setup.direction.value,
                    entry_price=setup.entry_price,
                    stop_price=setup.stop_price,
                    target_price=setup.target_price,
                    qty=qty, lot_size=cfg.lot_size,
                    entry_time=fill_time,
                    entry_reason=setup.entry_reason,
                    balance_at_entry=balance,
                    last_30m_time=current_30m,
                )
                remaining = _get_scan_slice(st.scan_df, fill_time, current_time)
                if remaining.empty:
                    remaining = bar_scan[bar_scan.index > fill_time]
                if not remaining.empty:
                    exit_trade = _scan_exit(st.position, remaining, cfg)
                    if exit_trade is not None:
                        exit_trade.id = trade_id
                        exit_trade.ticker = symbol
                        trade_id += 1
                        trades_by_ticker[symbol].append(exit_trade)
                        balance += exit_trade.pnl
                        st.consecutive_sl, st.sl_date = _update_sl_counter(
                            exit_trade.exit_reason, st.consecutive_sl,
                            st.sl_date, exit_trade.exit_time,
                        )
                        st.position = None
            else:
                st.pending = (setup, qty, balance)
                st.pending_30m_count = 0
                st.pending_last_30m = current_30m

        # Close remaining positions at last price
        for symbol, st in states.items():
            if st.position is not None:
                last_price = float(st.primary_df.iloc[-1]["close"])
                last_price = _apply_slippage(last_price, st.position.direction, config.slippage_pct)
                exit_trade = _close_position(
                    st.position, last_price, "end_of_data", st.primary_df.index[-1], config,
                )
                exit_trade.id = trade_id
                exit_trade.ticker = symbol
                trade_id += 1
                trades_by_ticker[symbol].append(exit_trade)
                balance += exit_trade.pnl

        # NOTE: warmup trades no longer need filtering — state is reset
        # at sim_start boundary (see reset block in event loop above).

        mode = "1m" if high_precision else "bar"
        logger.info(
            "[RUNNER INTERLEAVED] %s mode, %d tickers, %d trades, balance=%.0f",
            mode, len(states),
            sum(len(t) for t in trades_by_ticker.values()),
            balance,
        )
        return trades_by_ticker, balance


@dataclass
class _InterleavedState:
    """Per-ticker mutable state for interleaved simulation."""
    ticker: str
    strategy: object
    strategy_name: str
    candles: dict[str, pd.DataFrame]
    primary_tf: str
    primary_df: pd.DataFrame
    scan_df: pd.DataFrame | None
    df_30m: pd.DataFrame | None
    lot_size: int
    price_step: float
    dividend_dates: list
    resample_cache: _ResampleCache | None = None
    position: _VirtualPosition | None = None
    pending: tuple | None = None
    pending_30m_count: int = 0
    pending_last_30m: pd.Timestamp | None = None
    consecutive_sl: int = 0
    sl_date: str = ""


# ── Helper functions ──────────────────────────────────────────

def _synthesize_scan_bar(primary_df: pd.DataFrame, i: int, current_time) -> pd.DataFrame:
    """Create synthetic scan data from a primary bar's OHLC.

    Generates 4 micro-bars simulating intra-bar price movement:
    Open → High → Low → Close (bullish bar) or Open → Low → High → Close (bearish).
    This allows fill/exit logic to work when high-resolution data is unavailable.
    """
    row = primary_df.iloc[i]
    o, h, l, c = float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"])
    vol = float(row.get("volume", 0)) / 4

    is_bullish = c >= o
    if is_bullish:
        # Open → Low → High → Close (typical bullish: dip then rally)
        prices = [
            (o, o, l, l),   # open → low
            (l, h, l, h),   # low → high
            (h, h, c, c),   # high → close
        ]
    else:
        # Open → High → Low → Close (typical bearish: rally then drop)
        prices = [
            (o, h, o, h),   # open → high
            (h, h, l, l),   # high → low
            (l, c, l, c),   # low → close
        ]

    ts = current_time
    records = []
    for j, (po, ph, pl, pc) in enumerate(prices):
        records.append({
            "open": po, "high": ph, "low": pl, "close": pc, "volume": vol,
        })

    df = pd.DataFrame(records, index=[ts] * len(records))
    # Deduplicate index by adding microsecond offsets
    new_idx = [ts + pd.Timedelta(microseconds=j) for j in range(len(records))]
    df.index = new_idx
    return df


def _get_scan_slice(
    scan_df: pd.DataFrame | None, start: pd.Timestamp, end: pd.Timestamp,
) -> pd.DataFrame:
    """Get high-resolution candles between two timestamps (exclusive start, inclusive end)."""
    if scan_df is None or scan_df.empty:
        return pd.DataFrame()
    return scan_df[(scan_df.index > start) & (scan_df.index <= end)]


def _get_30m_at(df_30m: pd.DataFrame | None, current_time: pd.Timestamp) -> pd.Timestamp | None:
    if df_30m is None or df_30m.empty:
        return None
    bars = df_30m[df_30m.index <= current_time]
    return bars.index[-1] if not bars.empty else None


def _scan_exit(
    pos: _VirtualPosition, scan_df: pd.DataFrame, cfg: SimulationConfig,
) -> TradeDisplayRecord | None:
    """Scan high-resolution candles for SL or TP hit.

    If candle gaps through SL (open already past stop), uses open as fill price + slippage.
    TP is limit order — no slippage, fills at TP or better.
    """
    if scan_df.empty:
        return None

    is_buy = pos.direction == "BUY"

    for ts, c in scan_df.iterrows():
        if is_buy:
            # SL check
            if c["low"] <= pos.stop_price:
                price = min(float(c["open"]), pos.stop_price)
                price = _apply_slippage(price, pos.direction, cfg.slippage_pct)
                return _close_position(pos, price, "stop_loss", ts, cfg)
            # TP check
            if c["high"] >= pos.target_price:
                price = max(float(c["open"]), pos.target_price)  # limit — no slippage
                return _close_position(pos, price, "take_profit", ts, cfg)
        else:
            # SL check
            if c["high"] >= pos.stop_price:
                price = max(float(c["open"]), pos.stop_price)
                price = _apply_slippage(price, pos.direction, cfg.slippage_pct)
                return _close_position(pos, price, "stop_loss", ts, cfg)
            # TP check
            if c["low"] <= pos.target_price:
                price = min(float(c["open"]), pos.target_price)  # limit — no slippage
                return _close_position(pos, price, "take_profit", ts, cfg)

    return None


def _scan_fill(
    setup, scan_df: pd.DataFrame, cfg: SimulationConfig,
) -> "pd.Timestamp | str | None":
    """Scan for limit order fill on high-resolution candles.

    Returns:
        Timestamp if filled, "invalidated" if SL hit before entry, None if not filled.
    """
    if scan_df is None or scan_df.empty:
        return None

    is_buy = setup.direction.value == "BUY"

    for ts, c in scan_df.iterrows():
        if is_buy:
            if c["low"] <= setup.stop_price:
                return "invalidated"
            if c["low"] <= setup.entry_price:
                return ts
        else:
            if c["high"] >= setup.stop_price:
                return "invalidated"
            if c["high"] >= setup.entry_price:
                return ts

    return None


def _close_position(
    pos: _VirtualPosition, exit_price: float, reason: str,
    exit_time, cfg: SimulationConfig,
) -> TradeDisplayRecord:
    """Close position and calculate P&L with commission and margin cost."""
    shares = pos.qty * pos.lot_size

    if pos.direction == "BUY":
        pnl = (exit_price - pos.entry_price) * shares
    else:
        pnl = (pos.entry_price - exit_price) * shares

    # Commission (entry + exit)
    avg_price = (pos.entry_price + exit_price) / 2
    commission = 2 * cfg.commission_pct * avg_price * shares

    # Margin overnight cost
    position_value = pos.entry_price * shares
    borrowed = max(0.0, position_value - pos.balance_at_entry)
    exit_dt = _to_datetime(exit_time)
    overnights = _count_overnights(pos.entry_time, exit_dt)
    margin_cost = _margin_overnight_cost(borrowed) * overnights

    pnl_net = pnl - commission - margin_cost

    return TradeDisplayRecord(
        ticker=pos.ticker,
        direction=pos.direction,
        entry_price=pos.entry_price,
        exit_price=exit_price,
        stop_price=pos.stop_price,
        target_price=pos.target_price,
        qty=pos.qty,
        pnl=round(pnl_net, 4),
        commission=round(commission + margin_cost, 4),
        entry_time=str(pos.entry_time),
        exit_time=str(exit_dt),
        entry_reason=pos.entry_reason,
        exit_reason=reason,
        candles_held=pos.candles_held,
    )


def _apply_slippage(price: float, direction: str, slippage_pct: float) -> float:
    """Worsen price by slippage for market orders (SL, timeout)."""
    if slippage_pct == 0.0:
        return price
    if direction == "BUY":
        return price * (1.0 - slippage_pct)  # sell lower
    else:
        return price * (1.0 + slippage_pct)  # buy higher


def _position_size(
    balance: float, entry: float, stop: float,
    risk_pct: float, max_pct: float, lot_size: int,
) -> int:
    """Calculate position size in lots (same as RiskManager)."""
    risk_per_share = abs(entry - stop)
    if risk_per_share == 0 or entry == 0:
        return 0

    risk_amount = balance * risk_pct
    max_shares = int(risk_amount / risk_per_share)

    max_by_balance = int((balance * max_pct) / (entry * lot_size))
    lots = min(max_shares // lot_size, max_by_balance)
    return max(lots, 0)


def _round_setup(setup, price_step: float):
    """Round setup prices to instrument price step."""
    from dataclasses import replace as dc_replace

    def _round(p: float) -> float:
        if price_step <= 0:
            return p
        return round(round(p / price_step) * price_step, 10)

    return dc_replace(
        setup,
        entry_price=_round(setup.entry_price),
        stop_price=_round(setup.stop_price),
        target_price=_round(setup.target_price),
    )


def _update_sl_counter(reason: str, consecutive_sl: int, sl_date: str, exit_time) -> tuple[int, str]:
    """Update consecutive SL counter (same logic as ExecutionManager)."""
    dt = _to_datetime(exit_time)
    today = dt.astimezone(MSK).date().isoformat() if dt.tzinfo else str(dt)[:10]

    if reason == "stop_loss":
        if sl_date == today:
            return consecutive_sl + 1, today
        return 1, today
    elif reason in ("take_profit", "timeout"):
        return 0, sl_date
    return consecutive_sl, sl_date


def _count_overnights(entry_time, exit_time) -> int:
    """Count midnight MSK crossings between entry and exit."""
    t_in = _to_datetime(entry_time).astimezone(MSK)
    t_out = _to_datetime(exit_time).astimezone(MSK)
    return max(0, (t_out.date() - t_in.date()).days)


def _margin_overnight_cost(borrowed: float) -> float:
    """T-Bank margin overnight cost per night (rubles)."""
    if borrowed <= 5_000:
        return 0.0
    elif borrowed <= 50_000:
        return 42.5
    elif borrowed <= 100_000:
        return 85.0
    elif borrowed <= 250_000:
        return 210.0
    elif borrowed <= 500_000:
        return 420.0
    elif borrowed <= 1_000_000:
        return 827.5
    elif borrowed <= 2_500_000:
        return 2_037.5
    elif borrowed <= 5_000_000:
        return 4_000.0
    else:
        return 7_775.0


def _get_last_price(scan_df: pd.DataFrame, primary_df: pd.DataFrame, i: int) -> float:
    """Get last available price from scan data or primary bar."""
    if scan_df is not None and not scan_df.empty:
        return float(scan_df.iloc[-1]["close"])
    return float(primary_df.iloc[i]["close"])


def _get_current_price(bar_scan: pd.DataFrame, primary_df: pd.DataFrame, i: int) -> float:
    """Get current market price at the moment of signal (open of scan bar or prev close)."""
    if bar_scan is not None and not bar_scan.empty:
        return float(bar_scan.iloc[0]["open"])
    if i > 0:
        return float(primary_df.iloc[i - 1]["close"])
    return 0.0


def _is_near_dividend(bar_date, dividend_dates: list, days_before: int = 3) -> bool:
    """Check if bar_date is within days_before of any dividend last_buy_date."""
    from datetime import date as date_type
    for d in dividend_dates:
        if isinstance(d, datetime):
            d = d.date()
        if not isinstance(d, date_type):
            continue
        diff = (d - bar_date).days
        if 0 <= diff <= days_before:
            return True
    return False




def _to_datetime(value) -> datetime:
    """Convert various time types to datetime."""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, pd.Timestamp):
        dt = value.to_pydatetime()
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    if isinstance(value, str):
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    return datetime.now(timezone.utc)
