"""Strategy simulation engine — mirrors backtest/engine.py with 15S precision.

Key differences from the chart's old runner:
- SL/TP/fill scanning on 15S candles (sub-minute precision)
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
_MIN_WARMUP = 60           # min bars before first signal
_PENDING_TIMEOUT = 20      # 30m-candle equivalents for limit order timeout


@dataclass
class SimulationConfig:
    """Configuration for the simulation engine."""
    initial_balance: float = 1_000_000.0
    risk_pct: float = 0.05
    max_position_pct: float = 0.40
    commission_pct: float = 0.0004
    slippage_pct: float = 0.0005
    max_candles_timeout: int = 48       # in 30m candles
    max_consecutive_sl: int = 3
    lot_size: int = 10                  # default, overridden per ticker
    price_step: float = 0.01            # default, overridden per ticker
    scan_tf: str = "15S"                # granularity for SL/TP/fill scanning


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
    1. Load strategy candles (30m, 1h) from TV — 5000 bars = months of data
    2. Load 15S scan candles from TV — loaded on-demand per time window
    3. Walk bar-by-bar on the primary strategy timeframe
    4. Inside each bar, scan 15S candles for precise SL/TP/fill
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

            # Sliding window (15 days, same as live)
            window_start = current_time - timedelta(days=_DAYS_WINDOW)
            window = {
                tf: df[(df.index > window_start) & (df.index <= current_time)]
                for tf, df in candles.items()
            }

            try:
                setup = strategy.find_setup(window)
            except Exception as e:
                result.errors.append(f"Error at bar {i}: {e}")
                continue

            if setup is None:
                continue

            result.setups_found += 1

            # Round prices to step
            if cfg.price_step > 0:
                setup = _round_setup(setup, cfg.price_step)

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
