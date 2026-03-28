import logging

import pandas as pd
from t_tech.invest import CandleInterval

from traderbot.broker.tbank import TBankBroker

logger = logging.getLogger(__name__)

MOEX_OPEN_HOUR_MSK = 7
MOEX_CLOSE_HOUR_MSK = 24  # до 23:59 включительно
MOEX_TIMEZONE = "Europe/Moscow"

# Маппинг строковых таймфреймов в частоты pandas для ресемплинга
_FREQ_MAP = {
    "1m": "1min",
    "30m": "30min",
    "1h": "1h",
    "4h": "4h",
}


class DataFeed:
    """Загрузка свечей и ресемплинг по таймфреймам."""

    def __init__(self, broker: TBankBroker):
        self.broker = broker

    def get_candles(self, figi: str, timeframes: list[str], days: int) -> dict[str, pd.DataFrame]:
        """
        Получить свечи для указанных таймфреймов (live trading).
        Загружает 1m свечи, ресемплит в нужные таймфреймы, фильтрует по часам MOEX.
        """
        df_1m = self.broker.get_candles(figi, CandleInterval.CANDLE_INTERVAL_1_MIN, days)
        if df_1m.empty:
            logger.warning("[FEED] No 1m candles for figi=%s", figi)
            return {}
        return self._resample_all(df_1m, timeframes)

    def get_candles_history(self, figi: str, timeframes: list[str], days: int) -> dict[str, pd.DataFrame]:
        """
        Получить свечи с пагинацией (для бэктеста).
        Загружает 1m свечи, ресемплит, фильтрует.
        """
        df_1m = self.broker.get_candles_history(figi, CandleInterval.CANDLE_INTERVAL_1_MIN, days)
        if df_1m.empty:
            logger.warning("[FEED] No 1m history for figi=%s", figi)
            return {}
        return self._resample_all(df_1m, timeframes)

    def _resample_all(self, df_1m: pd.DataFrame, timeframes: list[str]) -> dict[str, pd.DataFrame]:
        """Ресемплить 1m данные во все запрошенные таймфреймы."""
        result = {}
        for tf in timeframes:
            if tf == "1m":
                df = df_1m.copy()
            else:
                df = resample(df_1m, _FREQ_MAP[tf])
            df = filter_moex_hours(df)
            result[tf] = df
        return result


def resample(df_1m: pd.DataFrame, freq: str) -> pd.DataFrame:
    """
    Ресемплинг 1m свечей в заданный таймфрейм.
    Агрегация: open=first, high=max, low=min, close=last, volume=sum.
    """
    df = df_1m.resample(freq).agg({
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    })
    df = df.dropna(subset=["open"])
    return df


def filter_moex_hours(df: pd.DataFrame) -> pd.DataFrame:
    """
    Оставить только свечи в рабочие часы MOEX.
    Рабочие часы: Пн-Пт, 07:00-23:59 МСК (UTC+3).
    """
    if df.empty:
        return df
    # Конвертировать индекс в московское время для фильтрации
    msk = df.index.tz_convert(MOEX_TIMEZONE)
    # Пн-Пт (weekday 0-4)
    weekday_mask = msk.weekday < 5
    # 07:00 - 23:59
    hour_mask = msk.hour >= MOEX_OPEN_HOUR_MSK
    # MOEX_CLOSE_HOUR_MSK = 24, значит hour < 24 всегда true, оставляем все часы >= 7
    return df[weekday_mask & hour_mask]
