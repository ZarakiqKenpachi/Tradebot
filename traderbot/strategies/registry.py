from traderbot.strategies.base import BaseStrategy
from traderbot.strategies.ict import ICTStrategy
from traderbot.strategies.ict_v2_sw4_rr2 import ICTStrategyV2Sw4Rr2
from traderbot.strategies.ict_v2_sw4_rr35 import ICTStrategyV2Sw4Rr35
from traderbot.strategies.ict_v2_sw10_rr2 import ICTStrategyV2Sw10Rr2
from traderbot.strategies.ict_v2_tester import ICTStrategyV2Tester
from traderbot.strategies.ict_gazp import ICTGazpStrategy
from traderbot.strategies.tatn_strat import ICTStrategyV3Pro
from traderbot.strategies.gmkn_pro_trend_strat import GMKNProTrendStrategy
from traderbot.strategies.nvtk_pro_strategy import NVTKProStrategy

STRATEGY_REGISTRY: dict[str, type[BaseStrategy]] = {
    "ict": ICTStrategy,
    "ict_v2_sw4_rr2": ICTStrategyV2Sw4Rr2,
    "ict_v2_sw4_rr35": ICTStrategyV2Sw4Rr35,
    "ict_v2_sw10_rr2": ICTStrategyV2Sw10Rr2,
    "ict_v2_tester": ICTStrategyV2Tester,
    "ict_gazp": ICTGazpStrategy,
    "tatn_strat": ICTStrategyV3Pro,
    "gmkn_pro_trend_strat": GMKNProTrendStrategy,
    "nvtk_pro_strategy": NVTKProStrategy,
}


def get_strategy(name: str) -> BaseStrategy:
    """Создать экземпляр стратегии по имени."""
    return STRATEGY_REGISTRY[name]()
