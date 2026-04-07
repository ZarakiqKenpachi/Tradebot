import os
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv


@dataclass
class AdminTokenConfig:
    """Один admin-токен из списка config.admin.tokens."""
    token_env: str          # имя env-переменной с токеном
    name: str               # человекочитаемое имя (для логов/БД)
    chat_id_env: str        # env-переменная с tg_chat_id этого админа


@dataclass
class AdminConfig:
    tokens: list[AdminTokenConfig]
    chat_ids_env: str       # CSV нескольких admin chat_id (для рассылки admin-сообщений)


@dataclass
class MarketDataConfig:
    token_env: str
    app_name: str = "TraderBot-MD"


@dataclass
class DatabaseConfig:
    path: str


@dataclass
class SubscriptionConfig:
    price_rub: float
    period_days: int
    provider: str           # "manual" | "yookassa" | ...


@dataclass
class TickerConfig:
    figi: str
    strategy: str
    lot_size: int = 1


@dataclass
class TelegramConfig:
    enabled: bool
    token: str
    admin_chat_ids: list[str]


@dataclass
class AppConfig:
    database: DatabaseConfig
    market_data: MarketDataConfig
    admin: AdminConfig
    subscription: SubscriptionConfig
    risk_pct: float
    max_position_pct: float
    max_consecutive_sl: int
    tickers: dict[str, TickerConfig]
    telegram: TelegramConfig
    journal_path: str
    poll_interval_sec: int
    max_candles_timeout: int
    commission_pct: float
    backtest_initial_balance: float
    backtest_days: int
    backtest_output_dir: str


def load_config(path: str = "config.yaml") -> AppConfig:
    """Загрузить конфиг из YAML и подставить значения из .env."""
    load_dotenv()

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    # Database
    db_cfg = DatabaseConfig(path=raw["database"]["path"])

    # Market data
    md_raw = raw["market_data"]
    md_token = os.environ.get(md_raw["token_env"], "")
    if not md_token:
        raise ValueError(f"Environment variable {md_raw['token_env']} is not set (market_data token)")
    market_data = MarketDataConfig(
        token_env=md_token,   # храним уже resolved токен
        app_name=md_raw.get("app_name", "TraderBot-MD"),
    )

    # Admin tokens
    admin_raw = raw["admin"]
    admin_token_configs = []
    for t in admin_raw["tokens"]:
        token_val = os.environ.get(t["token_env"], "")
        if not token_val:
            raise ValueError(f"Environment variable {t['token_env']} is not set (admin token)")
        admin_token_configs.append(AdminTokenConfig(
            token_env=token_val,   # resolved токен
            name=t["name"],
            chat_id_env=t.get("chat_id_env", ""),
        ))
    admin = AdminConfig(
        tokens=admin_token_configs,
        chat_ids_env=admin_raw.get("chat_ids_env", ""),
    )

    # Risk
    risk = raw["risk"]

    # Tickers
    tickers = {}
    for name, conf in raw["tickers"].items():
        tickers[name] = TickerConfig(
            figi=conf["figi"],
            strategy=conf["strategy"],
            lot_size=conf.get("lot_size", 1),
        )

    # Telegram
    tg_raw = raw["notifications"]["telegram"]
    tg_enabled = tg_raw["enabled"]
    tg_token = os.environ.get(tg_raw["token_env"], "") if tg_enabled else ""
    if tg_enabled and not tg_token:
        raise ValueError(f"Environment variable {tg_raw['token_env']} is not set (telegram token)")

    # Admin chat IDs: из TELEGRAM_ADMIN_CHAT_ID (CSV)
    admin_chat_id_raw = os.environ.get(admin_raw.get("chat_ids_env", ""), "") if tg_enabled else ""
    admin_chat_ids = [x.strip() for x in admin_chat_id_raw.split(",") if x.strip()]

    telegram = TelegramConfig(
        enabled=tg_enabled,
        token=tg_token,
        admin_chat_ids=admin_chat_ids,
    )

    # Subscription
    sub_raw = raw.get("subscription", {})
    subscription = SubscriptionConfig(
        price_rub=sub_raw.get("price_rub", 0),
        period_days=sub_raw.get("period_days", 30),
        provider=sub_raw.get("provider", "manual"),
    )

    # Trading
    trading = raw["trading"]

    # Backtest
    bt = raw["backtest"]

    return AppConfig(
        database=db_cfg,
        market_data=market_data,
        admin=admin,
        subscription=subscription,
        risk_pct=risk["risk_pct"],
        max_position_pct=risk["max_position_pct"],
        max_consecutive_sl=risk["max_consecutive_sl"],
        tickers=tickers,
        telegram=telegram,
        journal_path=raw["journal"]["path"],
        poll_interval_sec=trading["poll_interval_sec"],
        max_candles_timeout=trading["max_candles_timeout"],
        commission_pct=trading["commission_pct"],
        backtest_initial_balance=bt["initial_balance"],
        backtest_days=bt["days"],
        backtest_output_dir=bt["output_dir"],
    )
