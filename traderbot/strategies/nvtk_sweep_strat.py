import logging
from zoneinfo import ZoneInfo

import pandas as pd

from traderbot.strategies.base import BaseStrategy
from traderbot.types import Setup, Signal

logger = logging.getLogger(__name__)

_MSK = ZoneInfo("Europe/Moscow")
_EVENING_HOUR = 17

# ── EMA light trend filter ───────────────────────────────────
EMA_PERIOD = 20

# ── Sweep ────────────────────────────────────────────────────
SWEEP_LOOKBACK = 4
MAX_SWEEP_AGE = 4

# ── Displacement quality ─────────────────────────────────────
DISPLACEMENT_MIN_BODY_RATIO = 0.50
DISPLACEMENT_MIN_ATR_RATIO = 0.80
MAX_DISP_BARS = 8

# ── Entry / Stop / Target ────────────────────────────────────
ENTRY_RETRACEMENT = 0.90
STOP_BUFFER = 0.004
MIN_SL_DISTANCE = 0.003
RISK_REWARD = 3.0


class NVTKSweepStrategy(BaseStrategy):
    """
    NVTK Sweep — sweep 6 свечей 1H, лёгкий EMA(20) тренд-фильтр, RR 1:2.
    Вечерний фильтр BUY.
    """

    required_timeframes = ["30m", "1h"]

    def __init__(self):
        self._pending_setup: Setup | None = None
        self._pending_sweep_level: float | None = None
        self._pending_direction: Signal | None = None

    def find_setup(self, candles):
        df_1h = candles["1h"]
        df_30m = candles["30m"]

        if len(df_1h) < max(SWEEP_LOOKBACK + 2, EMA_PERIOD + 1):
            return None

        if self._pending_setup is not None:
            if self._is_pending_invalidated(df_30m):
                self._clear_pending()
            else:
                return self._pending_setup

        sweep = self._detect_sweep(df_1h)
        if sweep is None:
            return None

        direction, sweep_level, sweep_time, sweep_wick = sweep

        # Лёгкий тренд-фильтр: close vs EMA(20)
        ema = df_1h["close"].ewm(span=EMA_PERIOD, adjust=False).mean()
        last_close = float(df_1h["close"].iloc[-1])
        last_ema = float(ema.iloc[-1])
        if direction == Signal.BUY and last_close < last_ema:
            return None
        if direction == Signal.SELL and last_close > last_ema:
            return None

        # Вечерний фильтр BUY
        if direction == Signal.BUY and not df_30m.empty:
            latest_time = df_30m.index[-1]
            try:
                msk_hour = latest_time.astimezone(_MSK).hour
            except Exception:
                msk_hour = latest_time.hour
            if msk_hour >= _EVENING_HOUR:
                return None

        setup = self._find_displacement(df_30m, direction, sweep_level, sweep_time, sweep_wick)
        if setup is not None:
            self._pending_setup = setup
            self._pending_sweep_level = sweep_level
            self._pending_direction = direction
        return setup

    def _detect_sweep(self, df_1h):
        n = len(df_1h)
        if n < SWEEP_LOOKBACK + 1:
            return None

        for offset in range(MAX_SWEEP_AGE):
            i = n - 1 - offset
            if i < SWEEP_LOOKBACK:
                break
            sweep_candle = df_1h.iloc[i]
            structure = df_1h.iloc[i - SWEEP_LOOKBACK:i]
            sweep_time = df_1h.index[i]

            structure_low = float(structure["low"].min())
            if sweep_candle["low"] < structure_low and sweep_candle["close"] > structure_low:
                return Signal.BUY, structure_low, sweep_time, float(sweep_candle["low"])

            structure_high = float(structure["high"].max())
            if sweep_candle["high"] > structure_high and sweep_candle["close"] < structure_high:
                return Signal.SELL, structure_high, sweep_time, float(sweep_candle["high"])

        return None

    def _find_displacement(self, df_30m, direction, sweep_level, sweep_time, sweep_wick):
        after_sweep = df_30m[df_30m.index >= sweep_time].iloc[:MAX_DISP_BARS]
        if after_sweep.empty:
            return None

        atr_14 = self._atr(df_30m, 14)
        if atr_14 is None or atr_14.empty:
            return None

        for idx, candle in after_sweep.iterrows():
            candle_range = float(candle["high"] - candle["low"])
            if candle_range == 0:
                continue

            body = abs(float(candle["close"] - candle["open"]))
            if body / candle_range < DISPLACEMENT_MIN_BODY_RATIO:
                continue

            candle_atr = atr_14.get(idx)
            if candle_atr is None or pd.isna(candle_atr) or candle_atr == 0:
                continue
            if candle_range < candle_atr * DISPLACEMENT_MIN_ATR_RATIO:
                continue

            if direction == Signal.BUY and candle["close"] > candle["open"]:
                entry_price = float(candle["close"]) - ENTRY_RETRACEMENT * body
                stop_price = sweep_wick * (1 - STOP_BUFFER)
                risk = entry_price - stop_price
                if risk > 0 and risk / entry_price >= MIN_SL_DISTANCE:
                    tp = entry_price + risk * RISK_REWARD
                    return Setup(
                        Signal.BUY,
                        round(entry_price, 4),
                        round(stop_price, 4),
                        round(tp, 4),
                        f"NVTK 1H свип ниже {sweep_level:.2f} в {sweep_time.strftime('%H:%M')}; "
                        f"30m BUY импульс в {idx.strftime('%H:%M')}",
                    )

            if direction == Signal.SELL and candle["close"] < candle["open"]:
                entry_price = float(candle["close"]) + ENTRY_RETRACEMENT * body
                stop_price = sweep_wick * (1 + STOP_BUFFER)
                risk = stop_price - entry_price
                if risk > 0 and risk / entry_price >= MIN_SL_DISTANCE:
                    tp = entry_price - risk * RISK_REWARD
                    return Setup(
                        Signal.SELL,
                        round(entry_price, 4),
                        round(stop_price, 4),
                        round(tp, 4),
                        f"NVTK 1H свип выше {sweep_level:.2f} в {sweep_time.strftime('%H:%M')}; "
                        f"30m SELL импульс в {idx.strftime('%H:%M')}",
                    )

        return None

    def _atr(self, df, p):
        if len(df) < p + 1:
            return None
        h = df["high"]
        l = df["low"]
        pc = df["close"].shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        return tr.rolling(p).mean()

    def _is_pending_invalidated(self, df_30m):
        if self._pending_direction is None or self._pending_sweep_level is None:
            return True
        if df_30m.empty:
            return True
        latest = df_30m.iloc[-1]
        if self._pending_direction == Signal.BUY:
            return float(latest["low"]) < self._pending_sweep_level
        return float(latest["high"]) > self._pending_sweep_level

    def on_trade_opened(self):
        self._clear_pending()

    def _clear_pending(self):
        self._pending_setup = None
        self._pending_sweep_level = None
        self._pending_direction = None
