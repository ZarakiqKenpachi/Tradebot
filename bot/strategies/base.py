from enum import Enum
from abc import ABC, abstractmethod


class Signal(Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class BaseStrategy(ABC):
    @abstractmethod
    def generate_signal(self, data) -> Signal:
        """Given market data, return a trading signal."""
        raise NotImplementedError
