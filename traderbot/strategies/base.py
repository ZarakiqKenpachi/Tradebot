from abc import ABC, abstractmethod

import pandas as pd

from traderbot.types import Setup


class BaseStrategy(ABC):
    """Базовый класс для всех торговых стратегий."""

    required_timeframes: list[str]

    @abstractmethod
    def find_setup(self, candles: dict[str, pd.DataFrame]) -> Setup | None:
        """
        Анализировать данные и найти торговый сетап.

        candles: словарь {таймфрейм: DataFrame}
        Возвращает Setup если найден валидный сетап, иначе None.
        """
        pass

    def on_trade_opened(self) -> None:
        """Вызывается движком после открытия позиции по этому сетапу."""
        pass
