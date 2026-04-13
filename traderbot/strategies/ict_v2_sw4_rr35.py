import logging

import pandas as pd

from traderbot.strategies.base import BaseStrategy
from traderbot.types import Setup, Signal

logger = logging.getLogger(__name__)

# ── Sweep ────────────────────────────────────────────────────
SWEEP_LOOKBACK = 8          # структура 8 часов (было 4 — ловил шум)

# ── EMA trend filter ─────────────────────────────────────────
EMA_FAST = 20
EMA_SLOW = 50
EMA_RANGE_THRESHOLD = 0.005

# ── Range-mode quality (строже тренда) ───────────────────────
RANGE_MIN_BODY_RATIO = 0.60
RANGE_MIN_ATR_RATIO  = 1.00
RANGE_SWEEP_AGE = 2

# ── Displacement quality ─────────────────────────────────────
DISPLACEMENT_MIN_BODY_RATIO = 0.45   # чуть мягче для большей частоты
DISPLACEMENT_MIN_ATR_RATIO  = 0.78   # чуть мягче для большей частоты
MAX_DISP_BARS = 8
MAX_SWEEP_AGE = 3                    # сканируем последние N свечей 1H на свип

# ── Entry / Stop / Target ─────────────────────────────────────
ENTRY_RETRACEMENT = 0.50
STOP_BUFFER       = 0.003
MIN_SL_DISTANCE   = 0.003
RISK_REWARD       = 2.0              # было 3.5 — недостижимо


class ICTStrategyV2Sw4Rr35(BaseStrategy):
    """
    VTBR ICT v2 — sweep 8 свечей 1H, EMA-тренд, RR 1:2.
    Изменения vs оригинал:
      - sweep_lookback 4→8 (качество структуры)
      - EMA20/50 на 1H — торгуем только по тренду
      - body_ratio 0.35→0.50, atr_ratio 0.70→0.85 (фильтрация мусора)
      - ограничение поиска displacement: 6 баров после свипа
      - RR 3.5→2.0 (TP теперь достигается)
      - стоп за фитилём свиповой свечи (не у структурного уровня)
    """

    required_timeframes = ["30m", "1h"]

    def __init__(self):
        self._pending_setup: Setup | None = None
        self._pending_sweep_level: float | None = None
        self._pending_direction: Signal | None = None

    # ── Main ─────────────────────────────────────────────────

    def find_setup(self, candles: dict[str, pd.DataFrame]) -> Setup | None:
        df_1h  = candles["1h"]
        df_30m = candles["30m"]

        if len(df_1h) < EMA_SLOW + SWEEP_LOOKBACK + 2:
            return None

        if self._pending_setup is not None:
            if self._is_pending_invalidated(df_30m):
                self._clear_pending()
            else:
                return self._pending_setup

        mode, trend = self._ema_mode(df_1h)
        if mode is None:
            return None

        is_range = (mode == "range")

        sweep = self._detect_sweep(df_1h, trend, is_range)
        if sweep is None:
            return None

        direction, sweep_level, sweep_time, sweep_wick = sweep

        setup = self._find_displacement(df_30m, direction, sweep_level, sweep_time, sweep_wick, is_range)
        if setup is not None:
            self._pending_setup = setup
            self._pending_sweep_level = sweep_level
            self._pending_direction = direction
        return setup

    # ── EMA mode ─────────────────────────────────────────────

    def _ema_mode(self, df_1h: pd.DataFrame):
        ema_fast = df_1h["close"].ewm(span=EMA_FAST, adjust=False).mean()
        ema_slow = df_1h["close"].ewm(span=EMA_SLOW, adjust=False).mean()
        ef = float(ema_fast.iloc[-1])
        es = float(ema_slow.iloc[-1])
        if pd.isna(ef) or pd.isna(es) or es == 0:
            return None, None
        spread = abs(ef - es) / es
        if spread <= EMA_RANGE_THRESHOLD:
            return "range", None
        if ef > es:
            return "trend", Signal.BUY
        return "trend", Signal.SELL

    # ── Sweep detection ───────────────────────────────────────

    def _detect_sweep(
        self, df_1h: pd.DataFrame, trend, is_range: bool
    ) -> tuple[Signal, float, pd.Timestamp, float] | None:
        n = len(df_1h)
        if n < SWEEP_LOOKBACK + 1:
            return None

        age = RANGE_SWEEP_AGE if is_range else MAX_SWEEP_AGE

        for offset in range(age):
            i = n - 1 - offset
            if i < SWEEP_LOOKBACK:
                break
            sweep_candle = df_1h.iloc[i]
            structure    = df_1h.iloc[i - SWEEP_LOOKBACK:i]
            sweep_time   = df_1h.index[i]

            check_buy  = is_range or trend == Signal.BUY
            check_sell = is_range or trend == Signal.SELL

            if check_buy:
                structure_low = float(structure["low"].min())
                if sweep_candle["low"] < structure_low and sweep_candle["close"] > structure_low:
                    return Signal.BUY, structure_low, sweep_time, float(sweep_candle["low"])

            if check_sell:
                structure_high = float(structure["high"].max())
                if sweep_candle["high"] > structure_high and sweep_candle["close"] < structure_high:
                    return Signal.SELL, structure_high, sweep_time, float(sweep_candle["high"])

        return None

    # ── Displacement ──────────────────────────────────────────

    def _find_displacement(
        self,
        df_30m: pd.DataFrame,
        direction: Signal,
        sweep_level: float,
        sweep_time: pd.Timestamp,
        sweep_wick: float,
        is_range: bool,
    ) -> Setup | None:
        after_sweep = df_30m[df_30m.index >= sweep_time].iloc[:MAX_DISP_BARS]
        if after_sweep.empty:
            return None

        atr_14 = self._calc_atr(df_30m, 14)
        if atr_14 is None or atr_14.empty:
            return None

        body_min = RANGE_MIN_BODY_RATIO if is_range else DISPLACEMENT_MIN_BODY_RATIO
        atr_min  = RANGE_MIN_ATR_RATIO  if is_range else DISPLACEMENT_MIN_ATR_RATIO

        for idx, candle in after_sweep.iterrows():
            candle_range = float(candle["high"] - candle["low"])
            if candle_range == 0:
                continue

            body = abs(float(candle["close"] - candle["open"]))

            if body / candle_range < body_min:
                continue

            candle_atr = atr_14.get(idx)
            if candle_atr is None or pd.isna(candle_atr) or candle_atr == 0:
                continue
            if candle_range < candle_atr * atr_min:
                continue

            if direction == Signal.BUY and candle["close"] > candle["open"]:
                entry_price = float(candle["close"]) - ENTRY_RETRACEMENT * body
                stop_price  = sweep_wick * (1 - STOP_BUFFER)
                risk = entry_price - stop_price
                if risk > 0 and risk / entry_price >= MIN_SL_DISTANCE:
                    target_price = entry_price + RISK_REWARD * risk
                    return Setup(
                        direction=Signal.BUY,
                        entry_price=round(entry_price, 4),
                        stop_price=round(stop_price, 4),
                        target_price=round(target_price, 4),
                        entry_reason=self._reason(Signal.BUY, sweep_level, sweep_time, idx, body, candle_range, float(candle_atr), is_range),
                    )

            if direction == Signal.SELL and candle["close"] < candle["open"]:
                entry_price = float(candle["close"]) + ENTRY_RETRACEMENT * body
                stop_price  = sweep_wick * (1 + STOP_BUFFER)
                risk = stop_price - entry_price
                if risk > 0 and risk / entry_price >= MIN_SL_DISTANCE:
                    target_price = entry_price - RISK_REWARD * risk
                    return Setup(
                        direction=Signal.SELL,
                        entry_price=round(entry_price, 4),
                        stop_price=round(stop_price, 4),
                        target_price=round(target_price, 4),
                        entry_reason=self._reason(Signal.SELL, sweep_level, sweep_time, idx, body, candle_range, float(candle_atr), is_range),
                    )

        return None

    # ── Helpers ───────────────────────────────────────────────

    def _calc_atr(self, df: pd.DataFrame, period: int) -> pd.Series | None:
        if len(df) < period + 1:
            return None
        h  = df["high"]
        l  = df["low"]
        pc = df["close"].shift(1)
        tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
        return tr.rolling(period).mean()

    def _is_pending_invalidated(self, df_30m: pd.DataFrame) -> bool:
        if self._pending_direction is None or self._pending_sweep_level is None:
            return True
        if df_30m.empty:
            return True
        latest = df_30m.iloc[-1]
        if self._pending_direction == Signal.BUY:
            return float(latest["low"]) < self._pending_sweep_level
        return float(latest["high"]) > self._pending_sweep_level

    def on_trade_opened(self) -> None:
        self._clear_pending()

    def _clear_pending(self):
        self._pending_setup    = None
        self._pending_sweep_level = None
        self._pending_direction   = None

    @staticmethod
    def _reason(
        direction: Signal,
        sweep_level: float,
        sweep_time: pd.Timestamp,
        candle_time: pd.Timestamp,
        body: float,
        candle_range: float,
        atr: float,
        is_range: bool = False,
    ) -> str:
        body_pct = body / candle_range * 100 if candle_range else 0
        atr_pct  = candle_range / atr * 100  if atr        else 0
        is_buy   = direction == Signal.BUY
        mode     = "боковик" if is_range else "тренд"
        return (
            f"VTBR ({mode}) 1H свип {'ниже' if is_buy else 'выше'} "
            f"{sweep_level:.2f} (sw{SWEEP_LOOKBACK}, {sweep_time.strftime('%H:%M')}); "
            f"30m импульс {'вверх' if is_buy else 'вниз'} в {candle_time.strftime('%H:%M')} "
            f"(тело {body_pct:.0f}%, {atr_pct:.0f}% ATR)"
        )
