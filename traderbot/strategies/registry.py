from traderbot.strategies.base import BaseStrategy
from traderbot.strategies.ict import ICTStrategy
from traderbot.strategies.ict_v2_sw4_rr2 import ICTStrategyV2Sw4Rr2
from traderbot.strategies.ict_v2_sw4_rr35 import ICTStrategyV2Sw4Rr35
from traderbot.strategies.ict_v2_sw10_rr2 import ICTStrategyV2Sw10Rr2
from traderbot.strategies.ict_v2_tester import ICTStrategyV2Tester

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "ict": ICTStrategy,
    "ict_v2_sw4_rr2": ICTStrategyV2Sw4Rr2,
    "ict_v2_sw4_rr35": ICTStrategyV2Sw4Rr35,
    "ict_v2_sw10_rr2": ICTStrategyV2Sw10Rr2,
    "ict_v2_tester": ICTStrategyV2Tester,
}


def get_strategy(name: str) -> BaseStrategy:
    """Создать экземпляр стратегии по имени."""
    return STRATEGY_REGISTRY[name]()
