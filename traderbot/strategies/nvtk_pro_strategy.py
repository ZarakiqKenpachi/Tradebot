import logging
from dataclasses import dataclass

import pandas as pd

from traderbot.strategies.base import BaseStrategy
from traderbot.types import Setup, Signal

logger = logging.getLogger(__name__)


EMA_FAST = 20
EMA_SLOW = 50
ATR_PERIOD = 14

TREND_LOOKBACK = 12
RANGE_LOOKBACK = 24
SWEEP_LOOKBACK = 10

MIN_BODY_RATIO = 0.55
MIN_ATR_RATIO = 1.0
MIN_REJECTION_WICK_RATIO = 0.50

STOP_BUFFER = 0.0045
MIN_SL_DISTANCE = 0.0030
ENTRY_BUFFER = 0.0005

RR_TREND = 2.2
RR_RANGE = 2.0

MAX_ZONE_ATTEMPTS = 1


class MarketRegime:
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE = "range"
    UNKNOWN = "unknown"


@dataclass
class CandidateSetup:
    setup: Setup
    zone_key: str
    invalidation_level: float
    direction: Signal


class NVTKProStrategy(BaseStrategy):
    """
    NVTK PRO v3
    - Имя класса не меняем
    - Registry менять не надо
    - Улучшен выход: target ближе и реалистичнее
    - Убираем часть "мертвых" сделок, которые раньше тухли в timeout
    """

    required_timeframes = ["15m", "30m", "1h"]

    def __init__(self):
        self._pending_candidate: CandidateSetup | None = None
        self._zone_attempts: dict[str, int] = {}

    # ============================================================
    # PUBLIC
    # ============================================================

    def find_setup(self, candles: dict[str, pd.DataFrame]) -> Setup | None:
        df_1h = candles["1h"].copy()
        df_30m = candles["30m"].copy()
        df_15m = candles["15m"].copy()

        if len(df_1h) < max(RANGE_LOOKBACK, EMA_SLOW + 5):
            return None
        if len(df_30m) < ATR_PERIOD + 20:
            return None
        if len(df_15m) < ATR_PERIOD + 20:
            return None

        self._prepare_indicators(df_1h, df_30m, df_15m)

        if self._pending_candidate is not None:
            if self._is_pending_invalidated(df_15m):
                self._clear_pending()
            else:
                return self._pending_candidate.setup

        regime = self._detect_regime(df_1h)

        candidate = None

        if regime == MarketRegime.TREND_UP:
            candidate = self._find_trend_continuation(df_1h, df_30m, df_15m, Signal.BUY)

        elif regime == MarketRegime.TREND_DOWN:
            candidate = self._find_trend_continuation(df_1h, df_30m, df_15m, Signal.SELL)

        if candidate is None and regime == MarketRegime.RANGE:
            candidate = self._find_range_reversal(df_1h, df_30m, df_15m)

        if candidate is None:
            return None

        if not self._can_trade_zone(candidate.zone_key):
            return None

        self._pending_candidate = candidate
        return candidate.setup

    def on_trade_opened(self) -> None:
        if self._pending_candidate is not None:
            zone_key = self._pending_candidate.zone_key
            self._zone_attempts[zone_key] = self._zone_attempts.get(zone_key, 0) + 1
        self._clear_pending()

    # ============================================================
    # INDICATORS
    # ============================================================

    def _prepare_indicators(
        self,
        df_1h: pd.DataFrame,
        df_30m: pd.DataFrame,
        df_15m: pd.DataFrame,
    ) -> None:
        df_1h["ema_fast"] = df_1h["close"].ewm(span=EMA_FAST, adjust=False).mean()
        df_1h["ema_slow"] = df_1h["close"].ewm(span=EMA_SLOW, adjust=False).mean()

        df_1h["atr"] = self._calc_atr(df_1h, ATR_PERIOD)
        df_30m["atr"] = self._calc_atr(df_30m, ATR_PERIOD)
        df_15m["atr"] = self._calc_atr(df_15m, ATR_PERIOD)

    def _calc_atr(self, df: pd.DataFrame, period: int) -> pd.Series:
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)

        tr = pd.concat(
            [
                high - low,
                (high - prev_close).abs(),
                (low - prev_close).abs(),
            ],
            axis=1,
        ).max(axis=1)

        return tr.rolling(period).mean()

    # ============================================================
    # REGIME
    # ============================================================

    def _detect_regime(self, df_1h: pd.DataFrame) -> str:
        recent = df_1h.tail(TREND_LOOKBACK)
        if len(recent) < TREND_LOOKBACK:
            return MarketRegime.UNKNOWN

        last = recent.iloc[-1]
        ema_fast = last["ema_fast"]
        ema_slow = last["ema_slow"]
        close = last["close"]

        if pd.isna(ema_fast) or pd.isna(ema_slow) or close == 0:
            return MarketRegime.UNKNOWN

        distance_pct = abs(ema_fast - ema_slow) / close

        highs = recent["high"].tolist()
        lows = recent["low"].tolist()

        hh = highs[-1] > highs[-3]
        hl = lows[-1] > lows[-3]
        lh = highs[-1] < highs[-3]
        ll = lows[-1] < lows[-3]

        if ema_fast > ema_slow and distance_pct >= 0.004 and hh and hl:
            return MarketRegime.TREND_UP

        if ema_fast < ema_slow and distance_pct >= 0.004 and lh and ll:
            return MarketRegime.TREND_DOWN

        return MarketRegime.RANGE

    # ============================================================
    # TREND CONTINUATION
    # ============================================================

    def _find_trend_continuation(
        self,
        df_1h: pd.DataFrame,
        df_30m: pd.DataFrame,
        df_15m: pd.DataFrame,
        direction: Signal,
    ) -> CandidateSetup | None:
        impulse = self._find_latest_impulse(df_30m, direction)
        if impulse is None:
            return None

        impulse_idx, impulse_candle = impulse

        impulse_open = impulse_candle["open"]
        impulse_close = impulse_candle["close"]
        impulse_high = impulse_candle["high"]
        impulse_low = impulse_candle["low"]

        body = abs(impulse_close - impulse_open)
        if body <= 0:
            return None

        body_low = min(impulse_open, impulse_close)
        body_high = max(impulse_open, impulse_close)

        zone_low = body_low + body * 0.55
        zone_high = body_low + body * 0.72

        recent_15m = df_15m.tail(4)

        if direction == Signal.BUY:
            touched = recent_15m["low"].min() <= zone_high and recent_15m["high"].max() >= zone_low
            if not touched:
                return None

            confirm = self._latest_confirm_candle(df_15m, Signal.BUY)
            if confirm is None:
                return None

            confirm_candle, confirm_idx = confirm
            if confirm_candle["close"] <= confirm_candle["open"]:
                return None

            entry = max(
                zone_low,
                min(
                    zone_high,
                    confirm_candle["close"] - (confirm_candle["close"] - confirm_candle["low"]) * 0.25,
                ),
            )
            entry *= (1 - ENTRY_BUFFER)

            stop = min(impulse_low, recent_15m["low"].min()) * (1 - STOP_BUFFER)
            if not self._valid_prices(entry, stop, Signal.BUY):
                return None

            target = self._build_smart_target(
                entry=entry,
                stop=stop,
                direction=Signal.BUY,
                rr=RR_TREND,
                df_1h=df_1h,
                df_30m=df_30m,
            )

            setup = Setup(
                direction=Signal.BUY,
                entry_price=round(entry, 4),
                stop_price=round(stop, 4),
                target_price=round(target, 4),
                entry_reason=(
                    f"NVTK тренд BUY: 30m импульс в {impulse_idx.strftime('%H:%M')} "
                    f"(зона OTE {zone_low:.2f}–{zone_high:.2f}); "
                    f"15m подтверждение в {confirm_idx.strftime('%H:%M')}"
                ),
            )
            zone_key = f"trend_buy_{impulse_idx}_{round(zone_low,1)}_{round(zone_high,1)}"
            return CandidateSetup(setup, zone_key, stop, Signal.BUY)

        if direction == Signal.SELL:
            touched = recent_15m["high"].max() >= zone_low and recent_15m["low"].min() <= zone_high
            if not touched:
                return None

            confirm = self._latest_confirm_candle(df_15m, Signal.SELL)
            if confirm is None:
                return None

            confirm_candle, confirm_idx = confirm
            if confirm_candle["close"] >= confirm_candle["open"]:
                return None

            entry = min(
                zone_high,
                max(
                    zone_low,
                    confirm_candle["close"] + (confirm_candle["high"] - confirm_candle["close"]) * 0.25,
                ),
            )
            entry *= (1 + ENTRY_BUFFER)

            stop = max(impulse_high, recent_15m["high"].max()) * (1 + STOP_BUFFER)
            if not self._valid_prices(entry, stop, Signal.SELL):
                return None

            target = self._build_smart_target(
                entry=entry,
                stop=stop,
                direction=Signal.SELL,
                rr=RR_TREND,
                df_1h=df_1h,
                df_30m=df_30m,
            )

            setup = Setup(
                direction=Signal.SELL,
                entry_price=round(entry, 4),
                stop_price=round(stop, 4),
                target_price=round(target, 4),
                entry_reason=(
                    f"NVTK тренд SELL: 30m импульс в {impulse_idx.strftime('%H:%M')} "
                    f"(зона OTE {zone_low:.2f}–{zone_high:.2f}); "
                    f"15m подтверждение в {confirm_idx.strftime('%H:%M')}"
                ),
            )
            zone_key = f"trend_sell_{impulse_idx}_{round(zone_low,1)}_{round(zone_high,1)}"
            return CandidateSetup(setup, zone_key, stop, Signal.SELL)

        return None

    def _find_latest_impulse(
        self,
        df_30m: pd.DataFrame,
        direction: Signal,
    ) -> tuple[pd.Timestamp, pd.Series] | None:
        recent = df_30m.tail(10)

        for idx, candle in reversed(list(recent.iterrows())):
            atr = candle["atr"]
            if pd.isna(atr) or atr <= 0:
                continue

            candle_range = candle["high"] - candle["low"]
            if candle_range <= 0:
                continue

            body = abs(candle["close"] - candle["open"])
            body_ratio = body / candle_range
            atr_ratio = candle_range / atr

            if body_ratio < MIN_BODY_RATIO:
                continue
            if atr_ratio < MIN_ATR_RATIO:
                continue

            if direction == Signal.BUY and candle["close"] > candle["open"]:
                return idx, candle

            if direction == Signal.SELL and candle["close"] < candle["open"]:
                return idx, candle

        return None

    # ============================================================
    # RANGE REVERSAL
    # ============================================================

    def _find_range_reversal(
        self,
        df_1h: pd.DataFrame,
        df_30m: pd.DataFrame,
        df_15m: pd.DataFrame,
    ) -> CandidateSetup | None:
        recent_1h = df_1h.tail(RANGE_LOOKBACK)
        if len(recent_1h) < RANGE_LOOKBACK:
            return None

        range_high = recent_1h["high"].max()
        range_low = recent_1h["low"].min()
        range_size = range_high - range_low
        if range_size <= 0:
            return None

        latest_30m = df_30m.iloc[-1]
        struct = df_30m.iloc[-(SWEEP_LOOKBACK + 1):-1]

        struct_high = struct["high"].max()
        struct_low = struct["low"].min()

        if latest_30m["high"] > struct_high and latest_30m["close"] < struct_high:
            if latest_30m["high"] < range_high - range_size * 0.10:
                return None

            if not self._passes_rejection_quality(latest_30m):
                return None

            entry = latest_30m["close"] + (latest_30m["high"] - latest_30m["close"]) * 0.20
            entry *= (1 + ENTRY_BUFFER)
            stop = latest_30m["high"] * (1 + STOP_BUFFER)

            if not self._valid_prices(entry, stop, Signal.SELL):
                return None

            target = self._build_smart_target(
                entry=entry,
                stop=stop,
                direction=Signal.SELL,
                rr=RR_RANGE,
                df_1h=df_1h,
                df_30m=df_30m,
            )

            setup = Setup(
                direction=Signal.SELL,
                entry_price=round(entry, 4),
                stop_price=round(stop, 4),
                target_price=round(target, 4),
                entry_reason=(
                    f"NVTK флэт SELL: диапазон {range_low:.2f}–{range_high:.2f}; "
                    f"отбой от максимума {struct_high:.2f} "
                    f"в {df_30m.index[-1].strftime('%H:%M')}"
                ),
            )
            zone_key = f"range_sell_{round(struct_high,1)}_{df_30m.index[-1]}"
            return CandidateSetup(setup, zone_key, stop, Signal.SELL)

        if latest_30m["low"] < struct_low and latest_30m["close"] > struct_low:
            if latest_30m["low"] > range_low + range_size * 0.10:
                return None

            if not self._passes_rejection_quality(latest_30m):
                return None

            entry = latest_30m["close"] - (latest_30m["close"] - latest_30m["low"]) * 0.20
            entry *= (1 - ENTRY_BUFFER)
            stop = latest_30m["low"] * (1 - STOP_BUFFER)

            if not self._valid_prices(entry, stop, Signal.BUY):
                return None

            target = self._build_smart_target(
                entry=entry,
                stop=stop,
                direction=Signal.BUY,
                rr=RR_RANGE,
                df_1h=df_1h,
                df_30m=df_30m,
            )

            setup = Setup(
                direction=Signal.BUY,
                entry_price=round(entry, 4),
                stop_price=round(stop, 4),
                target_price=round(target, 4),
                entry_reason=(
                    f"NVTK флэт BUY: диапазон {range_low:.2f}–{range_high:.2f}; "
                    f"отбой от минимума {struct_low:.2f} "
                    f"в {df_30m.index[-1].strftime('%H:%M')}"
                ),
            )
            zone_key = f"range_buy_{round(struct_low,1)}_{df_30m.index[-1]}"
            return CandidateSetup(setup, zone_key, stop, Signal.BUY)

        return None

    # ============================================================
    # CONFIRM / QUALITY
    # ============================================================

    def _latest_confirm_candle(
        self,
        df_15m: pd.DataFrame,
        direction: Signal,
    ) -> tuple[pd.Series, pd.Timestamp] | None:
        recent = df_15m.tail(4)

        for idx, candle in reversed(list(recent.iterrows())):
            if not self._passes_impulse_quality(candle):
                continue

            if direction == Signal.BUY and candle["close"] > candle["open"]:
                return candle, idx

            if direction == Signal.SELL and candle["close"] < candle["open"]:
                return candle, idx

        return None

    def _passes_impulse_quality(self, candle: pd.Series) -> bool:
        atr = candle.get("atr")
        if pd.isna(atr) or atr <= 0:
            return False

        candle_range = candle["high"] - candle["low"]
        if candle_range <= 0:
            return False

        body = abs(candle["close"] - candle["open"])
        body_ratio = body / candle_range
        atr_ratio = candle_range / atr

        return body_ratio >= MIN_BODY_RATIO and atr_ratio >= MIN_ATR_RATIO

    def _passes_rejection_quality(self, candle: pd.Series) -> bool:
        candle_range = candle["high"] - candle["low"]
        if candle_range <= 0:
            return False

        atr = candle.get("atr")
        if pd.isna(atr) or atr <= 0:
            return False

        body = abs(candle["close"] - candle["open"])
        body_ratio = body / candle_range
        wick_ratio = 1 - body_ratio
        atr_ratio = candle_range / atr

        return wick_ratio >= MIN_REJECTION_WICK_RATIO and atr_ratio >= 0.9

    # ============================================================
    # TARGET / HELPERS
    # ============================================================

    def _build_smart_target(
        self,
        entry: float,
        stop: float,
        direction: Signal,
        rr: float,
        df_1h: pd.DataFrame,
        df_30m: pd.DataFrame,
    ) -> float:
        rr_target = self._build_rr_target(entry, stop, direction, rr)
        structure_target = self._find_nearest_structure_target(df_1h, df_30m, entry, direction)

        if structure_target is None:
            return rr_target

        if direction == Signal.BUY:
            if structure_target <= entry:
                return rr_target
            return min(rr_target, structure_target)

        if structure_target >= entry:
            return rr_target
        return max(rr_target, structure_target)

    def _find_nearest_structure_target(
        self,
        df_1h: pd.DataFrame,
        df_30m: pd.DataFrame,
        entry: float,
        direction: Signal,
    ) -> float | None:
        levels = []

        levels.extend(df_30m.tail(12)["high"].tolist())
        levels.extend(df_30m.tail(12)["low"].tolist())
        levels.extend(df_1h.tail(8)["high"].tolist())
        levels.extend(df_1h.tail(8)["low"].tolist())

        unique_levels = sorted(set(round(float(x), 4) for x in levels if pd.notna(x)))

        if direction == Signal.BUY:
            candidates = [lvl for lvl in unique_levels if lvl > entry]
            if not candidates:
                return None
            return candidates[min(2, len(candidates) - 1)]

        candidates = [lvl for lvl in unique_levels if lvl < entry]
        if not candidates:
            return None
        candidates = sorted(candidates, reverse=True)
        return candidates[min(2, len(candidates) - 1)]

    def _build_rr_target(
        self,
        entry: float,
        stop: float,
        direction: Signal,
        rr: float,
    ) -> float:
        if direction == Signal.BUY:
            risk = entry - stop
            return entry + risk * rr

        risk = stop - entry
        return entry - risk * rr

    def _valid_prices(self, entry: float, stop: float, direction: Signal) -> bool:
        if entry <= 0 or stop <= 0:
            return False

        if direction == Signal.BUY:
            risk = entry - stop
            if risk <= 0:
                return False
            return (risk / entry) >= MIN_SL_DISTANCE

        risk = stop - entry
        if risk <= 0:
            return False
        return (risk / entry) >= MIN_SL_DISTANCE

    def _can_trade_zone(self, zone_key: str) -> bool:
        return self._zone_attempts.get(zone_key, 0) < MAX_ZONE_ATTEMPTS

    def _is_pending_invalidated(self, df_15m: pd.DataFrame) -> bool:
        if self._pending_candidate is None or df_15m.empty:
            return True

        latest = df_15m.iloc[-1]
        pending = self._pending_candidate

        if pending.direction == Signal.BUY:
            return latest["low"] < pending.invalidation_level

        return latest["high"] > pending.invalidation_level

    def _clear_pending(self) -> None:
        self._pending_candidate = None
