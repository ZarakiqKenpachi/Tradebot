from dataclasses import dataclass
from typing import Optional

import pandas as pd

from bot.strategies.base import BaseStrategy, Signal

# 1H: how many candles to look back when identifying the liquidity level
SWEEP_LOOKBACK = 15          # sits in the 10–20 range requested
DISPLACEMENT_MIN_BODY_RATIO = 0.35
ENTRY_RETRACEMENT = 0.50     # limit at 50% retracement of the impulse candle
STOP_BUFFER = 0.003          # 0.3% beyond the swept level
MIN_SL_DISTANCE = 0.003      # Minimum SL distance: 0.3% of entry price
DEFAULT_RISK_REWARD = 3.5


@dataclass
class ICTSetup:
    direction: Signal
    entry_price: float
    stop_price: float
    target_price: float
    sweep_level: float


class ICTStrategy(BaseStrategy):
    """
    ICT System — Variant A

    1H (15 candles):
        Build a structure window of the last 15 closed candles.
        Detect a sweep: the most recent candle's wick breaks the structure
        high/low but the candle closes back inside.
        The swept level becomes the stop reference.

    30m (after sweep open):
        Once a sweep is confirmed on 1H, look at 30m candles that opened
        AFTER the sweep candle's open time. Find the first displacement
        candle (body >= 35% of range) in the direction of the trade.
        That candle's 50% retracement becomes the limit entry.
    """

    def __init__(self, risk_reward: float = DEFAULT_RISK_REWARD):
        self.risk_reward = risk_reward

    def generate_signal(self, data) -> Signal:
        setup = self.find_setup(data["1h"], data["30m"])
        return Signal.HOLD if setup is None else setup.direction

    def find_setup(self, df_1h: pd.DataFrame, df_30m: pd.DataFrame) -> Optional[ICTSetup]:
        # Need at least SWEEP_LOOKBACK + 1 candles (structure + the sweep candle)
        if len(df_1h) < SWEEP_LOOKBACK + 1:
            return None

        sweep_candle = df_1h.iloc[-1]
        structure    = df_1h.iloc[-(SWEEP_LOOKBACK + 1):-1]  # 15 candles before it

        sweep_time = df_1h.index[-1]

        # --- Bullish sweep: wick below 15-candle structure low, close back above ---
        structure_low = structure["low"].min()
        if sweep_candle["low"] < structure_low and sweep_candle["close"] > structure_low:
            setup = self._find_displacement(df_30m, Signal.BUY, structure_low, sweep_time)
            if setup:
                return setup

        # --- Bearish sweep: wick above 15-candle structure high, close back below ---
        structure_high = structure["high"].max()
        if sweep_candle["high"] > structure_high and sweep_candle["close"] < structure_high:
            setup = self._find_displacement(df_30m, Signal.SELL, structure_high, sweep_time)
            if setup:
                return setup

        return None

    def _find_displacement(
        self,
        df_30m: pd.DataFrame,
        direction: Signal,
        sweep_level: float,
        sweep_time: pd.Timestamp,
    ) -> Optional[ICTSetup]:
        """
        Search 30m candles that opened at or after the sweep candle's open time.
        Find the first candle with body >= 35% of range in the trade direction.
        """
        # Only consider 30m candles inside / after the sweep 1H candle
        after_sweep = df_30m[df_30m.index >= sweep_time]

        if len(after_sweep) < 1:
            return None

        for _, candle in after_sweep.iterrows():
            candle_range = candle["high"] - candle["low"]
            if candle_range == 0:
                continue

            body = abs(candle["close"] - candle["open"])
            if body / candle_range < DISPLACEMENT_MIN_BODY_RATIO:
                continue

            if direction == Signal.BUY and candle["close"] > candle["open"]:
                entry_price = candle["close"] - ENTRY_RETRACEMENT * body
                stop_price  = sweep_level * (1 - STOP_BUFFER)
                risk = entry_price - stop_price
                if risk <= 0 or risk / entry_price < MIN_SL_DISTANCE:
                    continue
                return ICTSetup(
                    direction=Signal.BUY,
                    entry_price=round(entry_price, 4),
                    stop_price=round(stop_price, 4),
                    target_price=round(entry_price + self.risk_reward * risk, 4),
                    sweep_level=sweep_level,
                )

            if direction == Signal.SELL and candle["close"] < candle["open"]:
                entry_price = candle["close"] + ENTRY_RETRACEMENT * body
                stop_price  = sweep_level * (1 + STOP_BUFFER)
                risk = stop_price - entry_price
                if risk <= 0 or risk / entry_price < MIN_SL_DISTANCE:
                    continue
                return ICTSetup(
                    direction=Signal.SELL,
                    entry_price=round(entry_price, 4),
                    stop_price=round(stop_price, 4),
                    target_price=round(entry_price - self.risk_reward * risk, 4),
                    sweep_level=sweep_level,
                )

        return None
