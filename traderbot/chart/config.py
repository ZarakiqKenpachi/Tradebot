"""Chart Analyzer configuration."""
from dataclasses import dataclass, field


@dataclass
class ChartTheme:
    """Color scheme for the chart."""
    name: str = "dark"
    background: str = "#131722"
    text: str = "#d1d4dc"
    grid: str = "#1e222d"
    border: str = "#2a2e39"
    crosshair: str = "#758696"
    up_color: str = "#26a69a"
    down_color: str = "#ef5350"
    volume_up: str = "#26a69a80"
    volume_down: str = "#ef535080"
    ema_fast_color: str = "#2962ff"
    ema_slow_color: str = "#ff6d00"
    marker_buy: str = "#26a69a"
    marker_sell: str = "#ef5350"
    marker_tp: str = "#2962ff"
    marker_sl: str = "#ff9800"


DARK_THEME = ChartTheme()

LIGHT_THEME = ChartTheme(
    name="light",
    background="#ffffff",
    text="#131722",
    grid="#f0f3fa",
    border="#d6dcde",
    crosshair="#9598a1",
    up_color="#26a69a",
    down_color="#ef5350",
    volume_up="#26a69a40",
    volume_down="#ef535040",
    ema_fast_color="#2962ff",
    ema_slow_color="#ff6d00",
    marker_buy="#26a69a",
    marker_sell="#ef5350",
    marker_tp="#2962ff",
    marker_sl="#ff9800",
)


# Timeframes shown in toolbar
# Sub-minute require TV Premium — shown only when provider.has_premium
TIMEFRAMES = [
    ("15S", "15s"),
    ("1m", "1m"),
    ("5m", "5m"),
    ("15m", "15m"),
    ("30m", "30m"),
    ("1h", "1H"),
    ("2h", "2H"),
    ("4h", "4H"),
    ("1d", "D"),
    ("1w", "W"),
]

SECONDS_TIMEFRAMES = {"1S", "5S", "10S", "15S", "30S", "45S"}

# Default number of bars to request per timeframe
BARS_PER_TIMEFRAME = {
    "1S": 5000,
    "5S": 5000,
    "10S": 5000,
    "15S": 5000,
    "30S": 5000,
    "45S": 5000,
    "1m": 5000,
    "5m": 5000,
    "15m": 5000,
    "30m": 5000,
    "1h": 5000,
    "2h": 3000,
    "4h": 2000,
    "1d": 1000,
    "1w": 500,
}

# Auto-refresh intervals in seconds
REFRESH_INTERVALS = {
    "1S": 1,
    "5S": 3,
    "10S": 5,
    "15S": 10,
    "30S": 15,
    "45S": 20,
    "1m": 10,
    "5m": 15,
    "15m": 30,
    "30m": 60,
    "1h": 120,
    "2h": 180,
    "4h": 300,
    "1d": 600,
    "1w": 900,

}


@dataclass
class AppConfig:
    """Application-level settings."""
    theme: ChartTheme = field(default_factory=lambda: ChartTheme())
    default_timeframe: str = "1h"
    default_symbol: str = "SBER"
    auto_refresh: bool = True
    cache_db_path: str = "data/chart_cache.db"
    window_width: int = 1400
    window_height: int = 900
