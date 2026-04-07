import logging
import pandas as pd

from traderbot.strategies.base import BaseStrategy
from traderbot.types import Setup, Signal

logger = logging.getLogger(__name__)

# 👉 изменили RR
RISK_REWARD = 1.8

DISPLACEMENT_MIN_BODY_RATIO = 0.55
DISPLACEMENT_MIN_ATR_RATIO = 0.9

ENTRY_RETRACEMENT = 0.65

STOP_BUFFER = 0.0025
MIN_SL_DISTANCE = 0.0015

MAX_30M_BARS = 8


class ICTStrategyV3Pro(BaseStrategy):
    required_timeframes = ["30m", "1h"]

    def find_setup(self, candles):
        df_1h = candles["1h"]
        df_30m = candles["30m"]

        if df_1h.empty or df_30m.empty:
            return None

        sweep = self._sweep(df_1h)
        if sweep is None:
            return None

        direction, level, time = sweep

        return self._entry(df_30m, direction, level, time)

    def _sweep(self, df):
        if len(df) < 6:
            return None

        for i in range(len(df)-1, len(df)-6, -1):
            c = df.iloc[i]
            prev = df.iloc[i-3:i]

            low = prev["low"].min()
            if c["low"] < low and c["close"] > low:
                return Signal.BUY, low, df.index[i]

            high = prev["high"].max()
            if c["high"] > high and c["close"] < high:
                return Signal.SELL, high, df.index[i]

        return None

    def _entry(self, df, direction, level, time):
        after = df[df.index >= time].iloc[:MAX_30M_BARS]

        atr = self._atr(df, 14)
        if atr is None:
            return None

        for idx, c in after.iterrows():
            r = float(c["high"] - c["low"])
            if r <= 0:
                continue

            body = abs(float(c["close"] - c["open"]))
            if body / r < DISPLACEMENT_MIN_BODY_RATIO:
                continue

            a = atr.get(idx)
            if a is None or pd.isna(a):
                continue

            if r < a * DISPLACEMENT_MIN_ATR_RATIO:
                continue

            if direction == Signal.BUY:
                if c["close"] <= c["open"]:
                    continue

                entry = float(c["close"]) - ENTRY_RETRACEMENT * body
                stop = level * (1 - STOP_BUFFER)
                risk = entry - stop

                if risk <= 0 or risk / entry < MIN_SL_DISTANCE:
                    continue

                tp = entry + risk * RISK_REWARD

                return Setup(
                    Signal.BUY,
                    round(entry, 4),
                    round(stop, 4),
                    round(tp, 4),
                    "V3 PRO"
                )

            else:
                if c["close"] >= c["open"]:
                    continue

                entry = float(c["close"]) + ENTRY_RETRACEMENT * body
                stop = level * (1 + STOP_BUFFER)
                risk = stop - entry

                if risk <= 0 or risk / entry < MIN_SL_DISTANCE:
                    continue

                tp = entry - risk * RISK_REWARD

                return Setup(
                    Signal.SELL,
                    round(entry, 4),
                    round(stop, 4),
                    round(tp, 4),
                    "V3 PRO"
                )

        return None

    def _atr(self, df, p):
        if len(df) < p + 1:
            return None

        h = df["high"]
        l = df["low"]
        pc = df["close"].shift(1)

        tr = pd.concat([
            h - l,
            (h - pc).abs(),
            (l - pc).abs()
        ], axis=1).max(axis=1)

        return tr.rolling(p).mean()