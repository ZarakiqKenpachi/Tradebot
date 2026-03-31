import logging

import pandas as pd

from traderbot.strategies.base import BaseStrategy
from traderbot.types import Setup, Signal

logger = logging.getLogger(__name__)

# Параметры системы
MIN_CANDLES = 15                        # Мин. количество свечей на каждом ТФ для принятия решения
SWEEP_LOOKBACK = 15                     # Кол-во 1H свечей для определения структуры
DISPLACEMENT_MIN_BODY_RATIO = 0.35      # Мин. соотношение тело/диапазон свечи
DISPLACEMENT_MIN_ATR_RATIO = 0.70       # Мин. диапазон displacement в долях ATR(14)
ENTRY_RETRACEMENT = 0.50                # Вход на 50% ретрейсмента тела импульсной свечи
STOP_BUFFER = 0.003                     # 0.3% буфер за уровнем свипа
MIN_SL_DISTANCE = 0.003                 # Мин. расстояние до SL (0.3% от цены)
RISK_REWARD = 2.0                       # Соотношение риск/прибыль (1:2)


class ICTStrategy(BaseStrategy):
    """
    ICT System — Variant A.
    Локальный sweep → импульс → возврат → вход → RR 2.
    TF анализа: 1H, TF входа: 30m.
    """

    required_timeframes = ["30m", "1h"]

    def __init__(self):
        self._pending_setup: Setup | None = None
        self._pending_sweep_level: float | None = None
        self._pending_direction: Signal | None = None

    def find_setup(self, candles: dict[str, pd.DataFrame]) -> Setup | None:
        df_1h = candles["1h"]
        df_30m = candles["30m"]

        # Проверить достаточность данных по 1H (решение принимается на этом ТФ)
        if len(df_1h) < MIN_CANDLES:
            logger.debug(
                "[ICT] Недостаточно данных: 1h=%d (минимум %d)",
                len(df_1h), MIN_CANDLES,
            )
            return None

        # Проверить повторный ретест (pending setup)
        if self._pending_setup is not None:
            if self._is_pending_invalidated(df_30m):
                self._clear_pending()
            else:
                return self._pending_setup

        # Шаг 1: Обнаружение свипа на 1H
        sweep = self._detect_sweep(df_1h)
        if sweep is None:
            return None

        direction, sweep_level, sweep_time = sweep

        # Шаг 2: Поиск displacement на 30m
        setup = self._find_displacement(df_30m, direction, sweep_level, sweep_time)
        if setup is not None:
            # Сохранить для повторного ретеста
            self._pending_setup = setup
            self._pending_sweep_level = sweep_level
            self._pending_direction = direction
        return setup

    def _detect_sweep(self, df_1h: pd.DataFrame) -> tuple[Signal, float, pd.Timestamp] | None:
        """Обнаружить свип на 1H. Возвращает (direction, sweep_level, sweep_time) или None."""
        if len(df_1h) < SWEEP_LOOKBACK + 1:
            return None

        sweep_candle = df_1h.iloc[-1]
        structure = df_1h.iloc[-(SWEEP_LOOKBACK + 1):-1]
        sweep_time = df_1h.index[-1]

        # Бычий свип: фитиль ниже структурного минимума, закрытие выше
        structure_low = structure["low"].min()
        if sweep_candle["low"] < structure_low and sweep_candle["close"] > structure_low:
            return Signal.BUY, structure_low, sweep_time

        # Медвежий свип: фитиль выше структурного максимума, закрытие ниже
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
        """Найти импульсную свечу (displacement) на 30m после свипа."""
        after_sweep = df_30m[df_30m.index >= sweep_time]
        if len(after_sweep) < 1:
            return None

        # ATR(14) на 30m
        atr_14 = self._calc_atr(df_30m, 14)
        if atr_14 is None or atr_14.empty:
            return None

        for idx, candle in after_sweep.iterrows():
            candle_range = candle["high"] - candle["low"]
            if candle_range == 0:
                continue

            body = abs(candle["close"] - candle["open"])

            # Фильтр: тело достаточно большое относительно диапазона
            if body / candle_range < DISPLACEMENT_MIN_BODY_RATIO:
                continue

            # Фильтр: диапазон достаточно большой относительно ATR
            # Используем ATR на момент этой свечи
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
                    reason = self._format_reason(
                        Signal.BUY, sweep_level, sweep_time, idx,
                        body, candle_range, candle_atr,
                    )
                    return Setup(
                        direction=Signal.BUY,
                        entry_price=round(entry_price, 4),
                        stop_price=round(stop_price, 4),
                        target_price=round(target_price, 4),
                        entry_reason=reason,
                    )

            if direction == Signal.SELL and candle["close"] < candle["open"]:
                entry_price = candle["close"] + ENTRY_RETRACEMENT * body
                stop_price = sweep_level * (1 + STOP_BUFFER)
                risk = stop_price - entry_price
                if risk > 0 and risk / entry_price >= MIN_SL_DISTANCE:
                    target_price = entry_price - RISK_REWARD * risk
                    reason = self._format_reason(
                        Signal.SELL, sweep_level, sweep_time, idx,
                        body, candle_range, candle_atr,
                    )
                    return Setup(
                        direction=Signal.SELL,
                        entry_price=round(entry_price, 4),
                        stop_price=round(stop_price, 4),
                        target_price=round(target_price, 4),
                        entry_reason=reason,
                    )

        return None

    def _calc_atr(self, df: pd.DataFrame, period: int) -> pd.Series | None:
        """Рассчитать ATR(period) на DataFrame."""
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
        """Проверить, не аннулирован ли pending setup (экстремум обновлён)."""
        if self._pending_direction is None or self._pending_sweep_level is None:
            return True
        if df_30m.empty:
            return True

        latest = df_30m.iloc[-1]
        if self._pending_direction == Signal.BUY:
            # Для LONG: новый low ниже sweep_level → аннулирование
            return latest["low"] < self._pending_sweep_level
        else:
            # Для SHORT: новый high выше sweep_level → аннулирование
            return latest["high"] > self._pending_sweep_level

    def on_trade_opened(self) -> None:
        self._clear_pending()

    def _clear_pending(self):
        self._pending_setup = None
        self._pending_sweep_level = None
        self._pending_direction = None

    @staticmethod
    def _format_reason(
        direction: Signal,
        sweep_level: float,
        sweep_time: pd.Timestamp,
        candle_time: pd.Timestamp,
        body: float,
        candle_range: float,
        atr: float,
    ) -> str:
        body_pct = body / candle_range * 100 if candle_range else 0
        atr_pct = candle_range / atr * 100 if atr else 0
        is_buy = direction == Signal.BUY
        sweep_dir = "ниже" if is_buy else "выше"
        structure_ext = "минимум" if is_buy else "максимум"
        impulse_dir = "вверх" if is_buy else "вниз"
        sweep_ts = sweep_time.strftime("%Y-%m-%d %H:%M")
        candle_ts = candle_time.strftime("%Y-%m-%d %H:%M")
        return (
            f"1H свип {sweep_dir} структурного {structure_ext}а {sweep_level:.2f} "
            f"(глубина {SWEEP_LOOKBACK} свечей, время свипа {sweep_ts}); "
            f"30m импульсная свеча {impulse_dir} в {candle_ts} "
            f"(тело {body_pct:.0f}% диапазона, диапазон {atr_pct:.0f}% ATR); "
            f"вход на {ENTRY_RETRACEMENT*100:.0f}% ретрейсменте импульсной свечи"
        )
