import os
from dataclasses import dataclass

import yaml
from dotenv import load_dotenv


@dataclass
class BrokerConfig:
    token: str
    sandbox: bool
    app_name: str


@dataclass
class TickerConfig:
    figi: str
    strategy: str
    lot_size: int = 1


@dataclass
class TelegramConfig:
    enabled: bool
    token: str
    chat_id: str


@dataclass
class AppConfig:
    broker: BrokerConfig
    risk_pct: float
    max_position_pct: float
    max_consecutive_sl: int
    tickers: dict[str, TickerConfig]
    telegram: TelegramConfig
    journal_path: str
    state_path: str
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

    # Broker
    is_sandbox = raw["broker"]["sandbox"]
    token_env = raw["broker"]["sandbox_token_env"] if is_sandbox else raw["broker"]["live_token_env"]
    broker_token = os.environ.get(token_env, "")
    if not broker_token:
        mode = "sandbox" if is_sandbox else "live"
        raise ValueError(f"Environment variable {token_env} is not set (mode: {mode})")
    broker = BrokerConfig(
        token=broker_token,
        sandbox=is_sandbox,
        app_name=raw["broker"]["app_name"],
    )

    # Risk
    risk = raw["risk"]
    risk_pct = risk["risk_pct"]
    max_position_pct = risk["max_position_pct"]
    max_consecutive_sl = risk["max_consecutive_sl"]

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
    tg_chat_id = os.environ.get(tg_raw["chat_id_env"], "") if tg_enabled else ""
    telegram = TelegramConfig(enabled=tg_enabled, token=tg_token, chat_id=tg_chat_id)

    # Trading
    trading = raw["trading"]

    # Backtest
    bt = raw["backtest"]

    return AppConfig(
        broker=broker,
        risk_pct=risk_pct,
        max_position_pct=max_position_pct,
        max_consecutive_sl=max_consecutive_sl,
        tickers=tickers,
        telegram=telegram,
        journal_path=raw["journal"]["path"],
        state_path=raw["state"]["path"],
        poll_interval_sec=trading["poll_interval_sec"],
        max_candles_timeout=trading["max_candles_timeout"],
        commission_pct=trading["commission_pct"],
        backtest_initial_balance=bt["initial_balance"],
        backtest_days=bt["days"],
        backtest_output_dir=bt["output_dir"],
    )
