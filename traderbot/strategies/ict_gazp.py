"""ICT GAZP System 3.2 — Multi-timeframe strategy for GAZP.

D1 trend -> 4H context/anti-chop -> 1H 2-candle impulse -> 15m 50% reaction + micro-structure -> entry
Models: A (sweep + shift), B (continuation).
TP: 3R (tp2) + 5R runner (tp3).
"""

import logging
from typing import NamedTuple

import pandas as pd

from traderbot.strategies.base import BaseStrategy
from traderbot.types import Setup, Signal

logger = logging.getLogger(__name__)

# ── D1 ──────────────────────────────────────────────────────
SWING_ORDER = 3          # candles on each side for swing detection
D1_MIN_SWINGS = 2        # need at least 2 swing highs + 2 swing lows

# ── 4H ──────────────────────────────────────────────────────
CHOP_CONSECUTIVE = 3     # 3+ small candles in a row = chop
CHOP_BODY_ATR = 0.3      # body < 0.3 x ATR(14) = small
H4_LOOKBACK = 5          # candles back for bias support check

# ── 1H ──────────────────────────────────────────────────────
SWEEP_LOOKBACK = 68      # ~4 trading days of H1 (17h/day x 4)
IMPULSE_MAX_AGE = 10     # max H1 candles back for valid impulse
SWEEP_WINDOW = 20        # candles before impulse to look for sweep

# ── 15m ─────────────────────────────────────────────────────
RETRACEMENT = 0.50
STOP_BUFFER = 0.002      # 0.2% buffer beyond invalidation level
MIN_SL_PCT = 0.003       # min stop distance as % of price

# ── Targets ─────────────────────────────────────────────────
RR_TP2 = 2.0
RR_TP3 = 4.0


class _ImpulseBlock(NamedTuple):
    high: float
    low: float
    end_time: pd.Timestamp
    model: str  # "sweep_shift" or "continuation"


class ICTGazpStrategy(BaseStrategy):
    """ICT GAZP 3.2 — D1->4H->1H->15m multi-TF model."""

    required_timeframes = ["15m", "1h", "4h", "1d"]

    def __init__(self):
        self._pending_setup: Setup | None = None
        self._pending_invalidation: float | None = None
        self._pending_direction: Signal | None = None

    # ── Main entry point ─────────────────────────────────────

    def find_setup(self, candles: dict[str, pd.DataFrame]) -> Setup | None:
        df_1d = candles.get("1d")
        df_4h = candles.get("4h")
        df_1h = candles.get("1h")
        df_15m = candles.get("15m")

        for label, df in [("1d", df_1d), ("4h", df_4h), ("1h", df_1h), ("15m", df_15m)]:
            if df is None or df.empty:
                logger.debug("[GAZP] Missing %s data", label)
                return None

        # Pending setup check
        if self._pending_setup is not None:
            if self._is_invalidated(df_15m):
                self._clear()
            else:
                return self._pending_setup

        # Step 1: D1 structural bias (None = neutral)
        bias = self._d1_bias(df_1d)

        # Step 2: 4H anti-chop
        if self._is_choppy(df_4h):
            logger.debug("[GAZP] 4H chop — skip")
            return None

        # Step 3: resolve working bias
        # D1 clear trend → full model (sweep+shift + continuation)
        # D1 neutral     → only continuation, bias taken from 4H
        if bias is None:
            bias = self._4h_bias(df_4h)
            if bias is None:
                logger.debug("[GAZP] D1 neutral + 4H neutral — skip")
                return None
            d1_neutral = True
            logger.debug("[GAZP] D1 neutral — using 4H bias %s (continuation only)", bias.value)
        else:
            d1_neutral = False
            if not self._4h_confirms(df_4h, bias):
                logger.debug("[GAZP] 4H against D1 bias — skip")
                return None

        # Step 4: 1H impulse block
        # neutral D1 → continuation only (no sweep+shift)
        impulse = self._find_impulse(df_1h, bias, continuation_only=d1_neutral)
        if impulse is None:
            logger.debug("[GAZP] No 1H impulse — skip")
            return None

        # Step 5: 15m entry (50% retrace + reaction + micro BOS)
        setup, invalidation = self._entry_15m(df_15m, impulse, bias)
        if setup is None:
            return None

        self._pending_setup = setup
        self._pending_invalidation = invalidation
        self._pending_direction = bias
        logger.info("[GAZP] %s setup via %s", bias.value, impulse.model)
        return setup

    # ── D1: structural trend ─────────────────────────────────

    def _d1_bias(self, df: pd.DataFrame) -> Signal | None:
        """Higher highs + higher lows = BUY, lower highs + lower lows = SELL."""
        min_len = 2 * SWING_ORDER + D1_MIN_SWINGS
        if len(df) < min_len:
            return None

        sh = self._swings(df["high"].values, SWING_ORDER, find_highs=True)
        sl = self._swings(df["low"].values, SWING_ORDER, find_highs=False)

        if len(sh) < D1_MIN_SWINGS or len(sl) < D1_MIN_SWINGS:
            return None

        hh = sh[-1] > sh[-2]  # higher high
        hl = sl[-1] > sl[-2]  # higher low
        lh = sh[-1] < sh[-2]  # lower high
        ll = sl[-1] < sl[-2]  # lower low

        if hh and hl:
            return Signal.BUY
        if lh and ll:
            return Signal.SELL
        return None

    @staticmethod
    def _swings(values, order: int, find_highs: bool) -> list[float]:
        """Find swing highs or lows (local extrema with `order` candles on each side)."""
        result = []
        for i in range(order, len(values) - order):
            is_swing = True
            for j in range(1, order + 1):
                if find_highs:
                    if values[i] <= values[i - j] or values[i] <= values[i + j]:
                        is_swing = False
                        break
                else:
                    if values[i] >= values[i - j] or values[i] >= values[i + j]:
                        is_swing = False
                        break
            if is_swing:
                result.append(values[i])
        return result

    # ── 4H: anti-chop + bias confirmation ────────────────────

    def _is_choppy(self, df: pd.DataFrame) -> bool:
        """3+ consecutive 4H candles with body < 0.3 x ATR(14)."""
        if len(df) < 14 + CHOP_CONSECUTIVE:
            return False
        atr = self._atr(df, 14)
        if atr is None:
            return False

        small_count = 0
        for idx in df.index[-CHOP_CONSECUTIVE:]:
            body = abs(df.at[idx, "close"] - df.at[idx, "open"])
            a = atr.get(idx)
            if a is None or pd.isna(a) or a == 0:
                return False
            if body < CHOP_BODY_ATR * a:
                small_count += 1
            else:
                small_count = 0
        return small_count >= CHOP_CONSECUTIVE

    def _4h_confirms(self, df: pd.DataFrame, bias: Signal) -> bool:
        """4H close trending in the direction of D1 bias."""
        if len(df) < H4_LOOKBACK + 1:
            return False
        now = df["close"].iloc[-1]
        past = df["close"].iloc[-(H4_LOOKBACK + 1)]
        return (now > past) if bias == Signal.BUY else (now < past)

    def _4h_bias(self, df: pd.DataFrame) -> Signal | None:
        """Derive bias from 4H structure when D1 is neutral."""
        if len(df) < H4_LOOKBACK + 1:
            return None
        now = df["close"].iloc[-1]
        past = df["close"].iloc[-(H4_LOOKBACK + 1)]
        if now > past:
            return Signal.BUY
        if now < past:
            return Signal.SELL
        return None

    # ── 1H: impulse block detection ──────────────────────────

    def _find_impulse(self, df: pd.DataFrame, bias: Signal, continuation_only: bool = False) -> _ImpulseBlock | None:
        """Try sweep+shift first (unless continuation_only), fallback to continuation."""
        if not continuation_only:
            imp = self._sweep_shift(df, bias)
            if imp is not None:
                return imp
        return self._continuation(df, bias)

    def _sweep_shift(self, df: pd.DataFrame, bias: Signal) -> _ImpulseBlock | None:
        """Model A: liquidity sweep then 2-candle directional shift."""
        n = len(df)
        if n < SWEEP_LOOKBACK + 2:
            return None

        start = max(1, n - IMPULSE_MAX_AGE)

        # Scan recent pairs (newest first)
        for i in range(n - 1, start - 1, -1):
            c1, c2 = df.iloc[i - 1], df.iloc[i]
            if not self._is_directional_pair(c1, c2, bias):
                continue

            # Build structure from candles before this pair
            struct_end = i - 1
            struct_start = max(0, struct_end - SWEEP_LOOKBACK)
            if struct_end - struct_start < 5:
                continue

            structure = df.iloc[struct_start:struct_end]

            if bias == Signal.BUY:
                level = structure["low"].min()
            else:
                level = structure["high"].max()

            # Look for sweep in the few candles just before the impulse pair
            sweep_from = max(struct_start, i - 1 - SWEEP_WINDOW)
            for j in range(i - 2, sweep_from - 1, -1):
                sc = df.iloc[j]
                if bias == Signal.BUY:
                    if sc["low"] < level and sc["close"] > level:
                        return self._make_block(df, i, "sweep_shift")
                else:
                    if sc["high"] > level and sc["close"] < level:
                        return self._make_block(df, i, "sweep_shift")

        return None

    def _continuation(self, df: pd.DataFrame, bias: Signal) -> _ImpulseBlock | None:
        """Model B: 2 consecutive directional H1 candles (no sweep required)."""
        n = len(df)
        start = max(1, n - IMPULSE_MAX_AGE)

        for i in range(n - 1, start - 1, -1):
            c1, c2 = df.iloc[i - 1], df.iloc[i]
            if self._is_directional_pair(c1, c2, bias):
                return self._make_block(df, i, "continuation")
        return None

    @staticmethod
    def _is_directional_pair(c1, c2, bias: Signal) -> bool:
        if bias == Signal.BUY:
            return c1["close"] > c1["open"] and c2["close"] > c2["open"]
        return c1["close"] < c1["open"] and c2["close"] < c2["open"]

    @staticmethod
    def _make_block(df: pd.DataFrame, end_idx: int, model: str) -> _ImpulseBlock:
        c1, c2 = df.iloc[end_idx - 1], df.iloc[end_idx]
        return _ImpulseBlock(
            high=max(c1["high"], c2["high"]),
            low=min(c1["low"], c2["low"]),
            end_time=df.index[end_idx],
            model=model,
        )

    # ── 15m: entry logic ─────────────────────────────────────

    def _entry_15m(
        self, df: pd.DataFrame, imp: _ImpulseBlock, bias: Signal,
    ) -> tuple[Setup | None, float | None]:
        """Find 50% retrace touch -> reaction -> micro BOS on 15m."""
        after = df[df.index > imp.end_time]
        if len(after) < 3:
            return None, None

        mid = (imp.high + imp.low) * RETRACEMENT

        for i in range(len(after) - 2):
            touch = after.iloc[i]
            react = after.iloc[i + 1]

            if bias == Signal.BUY:
                # Touch: candle low reaches 50% zone
                if touch["low"] > mid:
                    continue
                # Reaction: next candle closes above midpoint
                if react["close"] <= mid:
                    continue
                # Micro BOS: subsequent candle breaks reaction high
                base_low = min(touch["low"], react["low"])
                for j in range(i + 2, len(after)):
                    m = after.iloc[j]
                    if m["low"] < base_low:
                        break  # zone invalidated
                    if m["high"] > react["high"]:
                        zone = after.iloc[i : j + 1]
                        local_low = zone["low"].min()
                        stop = round(local_low * (1 - STOP_BUFFER), 2)
                        risk = mid - stop
                        if risk <= 0 or risk / mid < MIN_SL_PCT:
                            break
                        tp2 = round(mid + RR_TP2 * risk, 2)
                        tp3 = round(mid + RR_TP3 * risk, 2)
                        return Setup(
                            direction=Signal.BUY,
                            entry_price=round(mid, 2),
                            stop_price=stop,
                            target_price=tp2,
                            entry_reason=f"GAZP 3.2: {imp.model}",
                            tp2=tp2,
                            tp3=tp3,
                        ), local_low

            else:  # SELL
                if touch["high"] < mid:
                    continue
                if react["close"] >= mid:
                    continue
                base_high = max(touch["high"], react["high"])
                for j in range(i + 2, len(after)):
                    m = after.iloc[j]
                    if m["high"] > base_high:
                        break
                    if m["low"] < react["low"]:
                        zone = after.iloc[i : j + 1]
                        local_high = zone["high"].max()
                        stop = round(local_high * (1 + STOP_BUFFER), 2)
                        risk = stop - mid
                        if risk <= 0 or risk / mid < MIN_SL_PCT:
                            break
                        tp2 = round(mid - RR_TP2 * risk, 2)
                        tp3 = round(mid - RR_TP3 * risk, 2)
                        return Setup(
                            direction=Signal.SELL,
                            entry_price=round(mid, 2),
                            stop_price=stop,
                            target_price=tp2,
                            entry_reason=f"GAZP 3.2: {imp.model}",
                            tp2=tp2,
                            tp3=tp3,
                        ), local_high

        return None, None

    # ── Pending setup management ─────────────────────────────

    def _is_invalidated(self, df_15m: pd.DataFrame) -> bool:
        if self._pending_direction is None or self._pending_invalidation is None:
            return True
        if df_15m.empty:
            return True
        last = df_15m.iloc[-1]
        if self._pending_direction == Signal.BUY:
            return last["close"] < self._pending_invalidation
        return last["close"] > self._pending_invalidation

    def on_trade_opened(self) -> None:
        self._clear()

    def _clear(self):
        self._pending_setup = None
        self._pending_invalidation = None
        self._pending_direction = None

    # ── Utility ──────────────────────────────────────────────

    @staticmethod
    def _atr(df: pd.DataFrame, period: int) -> pd.Series | None:
        if len(df) < period + 1:
            return None
        h, l, pc = df["high"], df["low"], df["close"].shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        return tr.rolling(period).mean()
