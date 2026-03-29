import logging

import pandas as pd

from traderbot.strategies.base import BaseStrategy
from traderbot.types import Setup, Signal

logger = logging.getLogger(__name__)

# =============================================
#  ПАРАМЕТРЫ ДЛЯ ТЕСТИРОВАНИЯ — меняй здесь
# =============================================
SWEEP_LOOKBACK = 4                      # Кол-во 1H свечей для структуры
RISK_REWARD = 2.0                       # Соотношение риск/прибыль
DISPLACEMENT_MIN_BODY_RATIO = 0.35      # Мин. тело/диапазон свечи
DISPLACEMENT_MIN_ATR_RATIO = 0.70       # Мин. диапазон в долях ATR(14)
ENTRY_RETRACEMENT = 0.50                # Вход на % ретрейсмента тела
STOP_BUFFER = 0.003                     # Буфер за уровнем свипа
MIN_SL_DISTANCE = 0.003                 # Мин. расстояние до SL
# =============================================


class ICTStrategyV2Tester(BaseStrategy):
    """ICT V2 — тестовая стратегия с настраиваемыми параметрами."""

    required_timeframes = ["30m", "1h"]

    def __init__(self):
        self._pending_setup: Setup | None = None
        self._pending_sweep_level: float | None = None
        self._pending_direction: Signal | None = None

    def find_setup(self, candles: dict[str, pd.DataFrame]) -> Setup | None:
        df_1h = candles["1h"]
        df_30m = candles["30m"]

        if self._pending_setup is not None:
            if self._is_pending_invalidated(df_30m):
                self._clear_pending()
            else:
                return self._pending_setup

        sweep = self._detect_sweep(df_1h)
        if sweep is None:
            return None

        direction, sweep_level, sweep_time = sweep

        setup = self._find_displacement(df_30m, direction, sweep_level, sweep_time)
        if setup is not None:
            self._pending_setup = setup
            self._pending_sweep_level = sweep_level
            self._pending_direction = direction
        return setup

    def _detect_sweep(self, df_1h: pd.DataFrame) -> tuple[Signal, float, pd.Timestamp] | None:
        if len(df_1h) < SWEEP_LOOKBACK + 1:
            return None

        sweep_candle = df_1h.iloc[-1]
        structure = df_1h.iloc[-(SWEEP_LOOKBACK + 1):-1]
        sweep_time = df_1h.index[-1]

        structure_low = structure["low"].min()
        if sweep_candle["low"] < structure_low and sweep_candle["close"] > structure_low:
            return Signal.BUY, structure_low, sweep_time

        structure_high = structure["high"].max()
        if sweep_candle["high"] > structure_high and sweep_candle["close"] < structure_high:
            return Signal.SELL, structure_high, sweep_time

        return None

    def _find_displacement(
        self,
        df_30m: pd.DataFrame,
        direction: Signal,
        sweep_level: float,
        sweep_time: pd.Timestamp,
    ) -> Setup | None:
        after_sweep = df_30m[df_30m.index >= sweep_time]
        if len(after_sweep) < 1:
            return None

        atr_14 = self._calc_atr(df_30m, 14)
        if atr_14 is None or atr_14.empty:
            return None

        for idx, candle in after_sweep.iterrows():
            candle_range = candle["high"] - candle["low"]
            if candle_range == 0:
                continue

            body = abs(candle["close"] - candle["open"])

            if body / candle_range < DISPLACEMENT_MIN_BODY_RATIO:
                continue

            candle_atr = atr_14.get(idx)
            if candle_atr is None or pd.isna(candle_atr) or candle_atr == 0:
                continue
            if candle_range < candle_atr * DISPLACEMENT_MIN_ATR_RATIO:
                continue

            if direction == Signal.BUY and candle["close"] > candle["open"]:
                entry_price = candle["close"] - ENTRY_RETRACEMENT * body
                stop_price = sweep_level * (1 - STOP_BUFFER)
                risk = entry_price - stop_price
                if risk > 0 and risk / entry_price >= MIN_SL_DISTANCE:
                    target_price = entry_price + RISK_REWARD * risk
                    return Setup(
                        direction=Signal.BUY,
                        entry_price=round(entry_price, 4),
                        stop_price=round(stop_price, 4),
                        target_price=round(target_price, 4),
                        entry_reason=f"TESTER: sweep {SWEEP_LOOKBACK}, RR {RISK_REWARD}",
                    )

            if direction == Signal.SELL and candle["close"] < candle["open"]:
                entry_price = candle["close"] + ENTRY_RETRACEMENT * body
                stop_price = sweep_level * (1 + STOP_BUFFER)
                risk = stop_price - entry_price
                if risk > 0 and risk / entry_price >= MIN_SL_DISTANCE:
                    target_price = entry_price - RISK_REWARD * risk
                    return Setup(
                        direction=Signal.SELL,
                        entry_price=round(entry_price, 4),
                        stop_price=round(stop_price, 4),
                        target_price=round(target_price, 4),
                        entry_reason=f"TESTER: sweep {SWEEP_LOOKBACK}, RR {RISK_REWARD}",
                    )

        return None

    def _calc_atr(self, df: pd.DataFrame, period: int) -> pd.Series | None:
        if len(df) < period + 1:
            return None
        high = df["high"]
        low = df["low"]
        prev_close = df["close"].shift(1)
        tr = pd.concat([
            high - low,
            (high - prev_close).abs(),
            (low - prev_close).abs(),
        ], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def _is_pending_invalidated(self, df_30m: pd.DataFrame) -> bool:
        if self._pending_direction is None or self._pending_sweep_level is None:
            return True
        if df_30m.empty:
            return True
        latest = df_30m.iloc[-1]
        if self._pending_direction == Signal.BUY:
            return latest["low"] < self._pending_sweep_level
        else:
            return latest["high"] > self._pending_sweep_level

    def on_trade_opened(self) -> None:
        self._clear_pending()

    def _clear_pending(self):
        self._pending_setup = None
        self._pending_sweep_level = None
        self._pending_direction = None
