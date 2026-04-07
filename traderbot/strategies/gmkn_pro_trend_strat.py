import logging

import pandas as pd

from traderbot.strategies.base import BaseStrategy
from traderbot.types import Setup, Signal

logger = logging.getLogger(__name__)

# ============================================================
# GMKN PRO TREND 2.0

RISK_REWARD = 2.4

EMA_FAST = 20
EMA_SLOW = 50

TREND_LOOKBACK_4H = 6
TREND_LOOKBACK_1H = 8
PULLBACK_LOOKBACK_30M = 8

DISPLACEMENT_MIN_BODY_RATIO = 0.50
DISPLACEMENT_MIN_ATR_RATIO = 0.80
DISPLACEMENT_BREAK_LOOKBACK = 4
MAX_BARS_AFTER_PULLBACK_FOR_SIGNAL = 6

OTE_MIN = 0.62
OTE_MAX = 0.79
OTE_ENTRY = 0.70

STOP_BUFFER = 0.0015
MIN_SL_DISTANCE = 0.0025
MAX_ENTRY_DISTANCE_FROM_LAST_CLOSE = 0.012

ATR_PERIOD = 14


class GMKNProTrendStrategy(BaseStrategy):
    """
    GMKN PRO:
    4H + 1H trend filter
    30m pullback
    15m displacement back into trend
    OTE entry
    """

    required_timeframes = ["15m", "30m", "1h", "4h"]

    def __init__(self):
        self._pending_setup: Setup | None = None
        self._pending_direction: Signal | None = None
        self._pending_invalidation_level: float | None = None
        self._last_pullback_time: pd.Timestamp | None = None
        self._attempts_on_pullback: int = 0

    def find_setup(self, candles: dict[str, pd.DataFrame]) -> Setup | None:
        df_15m = candles["15m"]
        df_30m = candles["30m"]
        df_1h = candles["1h"]
        df_4h = candles["4h"]

        if any(df.empty for df in (df_15m, df_30m, df_1h, df_4h)):
            return None

        if self._pending_setup is not None:
            if self._is_pending_invalidated(df_15m):
                self._clear_pending()
            else:
                return self._pending_setup

        trend = self._get_trend(df_4h, df_1h)
        if trend is None:
            return None

        pullback = self._detect_pullback(df_30m, trend)
        if pullback is None:
            return None

        pullback_time = pullback["time"]
        if self._last_pullback_time is None or pullback_time != self._last_pullback_time:
            self._last_pullback_time = pullback_time
            self._attempts_on_pullback = 0

        if self._attempts_on_pullback >= 2:
            return None

        setup = self._find_entry_on_15m(
            df_15m=df_15m,
            trend=trend,
            pullback_info=pullback,
        )
        if setup is not None:
            self._pending_setup = setup
            self._pending_direction = setup.direction
            self._pending_invalidation_level = setup.stop_price

        return setup

    def _get_trend(self, df_4h: pd.DataFrame, df_1h: pd.DataFrame) -> Signal | None:
        if len(df_4h) < max(EMA_SLOW, TREND_LOOKBACK_4H + 2):
            return None
        if len(df_1h) < max(EMA_SLOW, TREND_LOOKBACK_1H + 2):
            return None

        ema20_4h = df_4h["close"].ewm(span=EMA_FAST, adjust=False).mean()
        ema50_4h = df_4h["close"].ewm(span=EMA_SLOW, adjust=False).mean()
        ema20_1h = df_1h["close"].ewm(span=EMA_FAST, adjust=False).mean()
        ema50_1h = df_1h["close"].ewm(span=EMA_SLOW, adjust=False).mean()

        last_4h = df_4h.iloc[-1]
        last_1h = df_1h.iloc[-1]

        structure_4h = self._simple_structure(df_4h, TREND_LOOKBACK_4H)
        structure_1h = self._simple_structure(df_1h, TREND_LOOKBACK_1H)

        bearish = (
            structure_4h == Signal.SELL
            and structure_1h == Signal.SELL
            and float(last_4h["close"]) < float(ema20_4h.iloc[-1]) < float(ema50_4h.iloc[-1])
            and float(last_1h["close"]) < float(ema20_1h.iloc[-1]) < float(ema50_1h.iloc[-1])
        )
        bullish = (
            structure_4h == Signal.BUY
            and structure_1h == Signal.BUY
            and float(last_4h["close"]) > float(ema20_4h.iloc[-1]) > float(ema50_4h.iloc[-1])
            and float(last_1h["close"]) > float(ema20_1h.iloc[-1]) > float(ema50_1h.iloc[-1])
        )

        if bearish:
            return Signal.SELL
        if bullish:
            return Signal.BUY
        return None

    def _simple_structure(self, df: pd.DataFrame, lookback: int) -> Signal | None:
        if len(df) < lookback + 2:
            return None

        recent = df.iloc[-lookback:]
        prev = df.iloc[-(lookback + 1):-1]

        recent_high = float(recent["high"].max())
        recent_low = float(recent["low"].min())
        prev_high = float(prev["high"].max())
        prev_low = float(prev["low"].min())
        last_close = float(df.iloc[-1]["close"])

        if recent_high <= prev_high and recent_low <= prev_low and last_close <= float(recent["close"].mean()):
            return Signal.SELL
        if recent_high >= prev_high and recent_low >= prev_low and last_close >= float(recent["close"].mean()):
            return Signal.BUY
        return None

    def _detect_pullback(self, df_30m: pd.DataFrame, trend: Signal) -> dict | None:
        if len(df_30m) < PULLBACK_LOOKBACK_30M + 2:
            return None

        recent = df_30m.iloc[-PULLBACK_LOOKBACK_30M:].copy()
        atr = self._calc_atr(df_30m, ATR_PERIOD)
        if atr is None or atr.empty:
            return None

        last_idx = recent.index[-1]
        atr_value = atr.get(last_idx)
        if atr_value is None or pd.isna(atr_value) or atr_value <= 0:
            return None

        bullish_candles = int((recent["close"] > recent["open"]).sum())
        bearish_candles = int((recent["close"] < recent["open"]).sum())

        if trend == Signal.SELL:
            moved_up = float(recent.iloc[-1]["high"]) - float(recent["low"].min())
            if bullish_candles < 2:
                return None
            if moved_up < float(atr_value) * 0.7:
                return None
            return {
                "time": recent.index[-1],
                "pullback_high": float(recent["high"].max()),
                "pullback_low": float(recent["low"].min()),
            }

        if trend == Signal.BUY:
            moved_down = float(recent["high"].max()) - float(recent.iloc[-1]["low"])
            if bearish_candles < 2:
                return None
            if moved_down < float(atr_value) * 0.7:
                return None
            return {
                "time": recent.index[-1],
                "pullback_high": float(recent["high"].max()),
                "pullback_low": float(recent["low"].min()),
            }

        return None

    def _find_entry_on_15m(
        self,
        df_15m: pd.DataFrame,
        trend: Signal,
        pullback_info: dict,
    ) -> Setup | None:
        signal_time = pullback_info["time"]
        after_pullback = df_15m[df_15m.index >= signal_time].copy()
        if after_pullback.empty:
            return None

        limited = after_pullback.iloc[:MAX_BARS_AFTER_PULLBACK_FOR_SIGNAL]
        atr_15m = self._calc_atr(df_15m, ATR_PERIOD)
        if atr_15m is None or atr_15m.empty:
            return None

        for idx, candle in limited.iterrows():
            candle_range = float(candle["high"] - candle["low"])
            if candle_range <= 0:
                continue

            body = abs(float(candle["close"] - candle["open"]))
            body_ratio = body / candle_range
            atr_value = atr_15m.get(idx)
            if atr_value is None or pd.isna(atr_value) or atr_value <= 0:
                continue

            if body_ratio < DISPLACEMENT_MIN_BODY_RATIO:
                continue
            if candle_range < float(atr_value) * DISPLACEMENT_MIN_ATR_RATIO:
                continue

            full_idx = df_15m.index.get_loc(idx)
            if isinstance(full_idx, slice) or full_idx < DISPLACEMENT_BREAK_LOOKBACK:
                continue

            structure = df_15m.iloc[full_idx - DISPLACEMENT_BREAK_LOOKBACK:full_idx]
            local_high = float(structure["high"].max())
            local_low = float(structure["low"].min())

            if trend == Signal.SELL:
                if not (float(candle["close"]) < float(candle["open"]) and float(candle["close"]) < local_low):
                    continue
                setup = self._build_sell_setup(df_15m, candle, idx, pullback_info)
                if setup is not None:
                    return setup

            if trend == Signal.BUY:
                if not (float(candle["close"]) > float(candle["open"]) and float(candle["close"]) > local_high):
                    continue
                setup = self._build_buy_setup(df_15m, candle, idx, pullback_info)
                if setup is not None:
                    return setup

        return None

    def _build_sell_setup(
        self,
        df_15m: pd.DataFrame,
        candle: pd.Series,
        idx: pd.Timestamp,
        pullback_info: dict,
    ) -> Setup | None:
        disp_open = float(candle["open"])
        disp_close = float(candle["close"])
        disp_high = float(candle["high"])
        disp_body = abs(disp_close - disp_open)

        if disp_body <= 0:
            return None

        entry_price = disp_close + disp_body * OTE_ENTRY
        stop_price = max(disp_high, pullback_info["pullback_high"]) * (1 + STOP_BUFFER)
        risk = stop_price - entry_price

        last_close = float(df_15m.iloc[-1]["close"])
        if last_close < entry_price * (1 - MAX_ENTRY_DISTANCE_FROM_LAST_CLOSE):
            return None
        if risk <= 0 or risk / entry_price < MIN_SL_DISTANCE:
            return None

        target_price = entry_price - RISK_REWARD * risk

        return Setup(
            direction=Signal.SELL,
            entry_price=round(entry_price, 4),
            stop_price=round(stop_price, 4),
            target_price=round(target_price, 4),
            entry_reason=(
                f"GMKN PRO SELL: 4H+1H bearish trend; "
                f"30m pullback completed by {pullback_info['time'].strftime('%Y-%m-%d %H:%M')}; "
                f"15m bearish displacement at {idx.strftime('%Y-%m-%d %H:%M')}; "
                f"entry in OTE zone ({int(OTE_MIN*100)}-{int(OTE_MAX*100)}%) of displacement body"
            ),
        )

    def _build_buy_setup(
        self,
        df_15m: pd.DataFrame,
        candle: pd.Series,
        idx: pd.Timestamp,
        pullback_info: dict,
    ) -> Setup | None:
        disp_open = float(candle["open"])
        disp_close = float(candle["close"])
        disp_low = float(candle["low"])
        disp_body = abs(disp_close - disp_open)

        if disp_body <= 0:
            return None

        entry_price = disp_close - disp_body * OTE_ENTRY
        stop_price = min(disp_low, pullback_info["pullback_low"]) * (1 - STOP_BUFFER)
        risk = entry_price - stop_price

        last_close = float(df_15m.iloc[-1]["close"])
        if last_close > entry_price * (1 + MAX_ENTRY_DISTANCE_FROM_LAST_CLOSE):
            return None
        if risk <= 0 or risk / entry_price < MIN_SL_DISTANCE:
            return None

        target_price = entry_price + RISK_REWARD * risk

        return Setup(
            direction=Signal.BUY,
            entry_price=round(entry_price, 4),
            stop_price=round(stop_price, 4),
            target_price=round(target_price, 4),
            entry_reason=(
                f"GMKN PRO BUY: 4H+1H bullish trend; "
                f"30m pullback completed by {pullback_info['time'].strftime('%Y-%m-%d %H:%M')}; "
                f"15m bullish displacement at {idx.strftime('%Y-%m-%d %H:%M')}; "
                f"entry in OTE zone ({int(OTE_MIN*100)}-{int(OTE_MAX*100)}%) of displacement body"
            ),
        )

    def _calc_atr(self, df: pd.DataFrame, period: int) -> pd.Series | None:
        if len(df) < period + 1:
            return None

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

    def _is_pending_invalidated(self, df_15m: pd.DataFrame) -> bool:
        if self._pending_direction is None or self._pending_invalidation_level is None:
            return True
        if df_15m.empty:
            return True

        latest = df_15m.iloc[-1]

        if self._pending_direction == Signal.BUY:
            return float(latest["low"]) < float(self._pending_invalidation_level)

        return float(latest["high"]) > float(self._pending_invalidation_level)

    def on_trade_opened(self) -> None:
        self._attempts_on_pullback += 1
        self._clear_pending()

    def _clear_pending(self) -> None:
        self._pending_setup = None
        self._pending_direction = None
        self._pending_invalidation_level = None
