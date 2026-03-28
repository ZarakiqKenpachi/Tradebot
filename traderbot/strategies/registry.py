from traderbot.strategies.base import BaseStrategy
from traderbot.strategies.ict import ICTStrategy
from traderbot.strategies.ict_v2 import ICTStrategyV2

STRATEGY_REGISTRY: dict[str, type[BaseStrategy] | tuple] = {
    "ict": ICTStrategy,
    "ict_v2": ICTStrategyV2,
    "ict_v2_sw4_rr2": (ICTStrategyV2, {"sweep_lookback": 4, "risk_reward": 2.0}),
    "ict_v2_sw4_rr35": (ICTStrategyV2, {"sweep_lookback": 4, "risk_reward": 3.5}),
    "ict_v2_sw10_rr2": (ICTStrategyV2, {"sweep_lookback": 10, "risk_reward": 2.0}),
    "ict_v2_sw10_rr35": (ICTStrategyV2, {"sweep_lookback": 10, "risk_reward": 3.5}),
}


def get_strategy(name: str) -> BaseStrategy:
    """Создать экземпляр стратегии по имени."""
    entry = STRATEGY_REGISTRY[name]
    if isinstance(entry, tuple):
        cls, kwargs = entry
        return cls(**kwargs)
    return entry()
