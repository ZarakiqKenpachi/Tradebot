"""
Точка входа для live-торговли.
Запуск: py -3.12 -m traderbot.main
"""
import logging
import time
from logging.handlers import RotatingFileHandler
import os

from traderbot.broker.tbank import TBankBroker
from traderbot.config import load_config
from traderbot.data.feed import DataFeed
from traderbot.execution.manager import ExecutionManager
from traderbot.journal.writer import TradeJournal
from traderbot.notifications.telegram import TelegramNotifier
from traderbot.risk.manager import RiskManager
from traderbot.state.store import StateStore
from traderbot.strategies.registry import get_strategy


def setup_logging():
    os.makedirs("logs", exist_ok=True)
    handler = RotatingFileHandler(
        "logs/bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    # 1. Загрузить конфиг
    import pathlib
    config_path = str(pathlib.Path(__file__).parent / "config.yaml")
    config = load_config(config_path)

    # 2. Инициализировать модули
    broker = TBankBroker(config.broker.token, config.broker.sandbox, config.broker.app_name)
    feed = DataFeed(broker)
    journal = TradeJournal(config.journal_path)
    state = StateStore(config.state_path)
    notifier = (
        TelegramNotifier(config.telegram.token, config.telegram.chat_id)
        if config.telegram.enabled else None
    )

    # 3. Подготовить аккаунт
    if config.broker.sandbox:
        account_id = broker.sandbox_create_account()
        broker.sandbox_fund(account_id, 100000.0)
    else:
        account_id = broker.get_account_id()

    # 4. Получить депозит из API (фиксируется один раз при старте)
    deposit = broker.get_portfolio_balance(account_id)
    logger.info("Депозит: %.2f RUB", deposit)
    risk = RiskManager(config.risk_pct, config.max_position_pct, deposit)

    # 5. Инициализировать стратегии для каждого тикера
    strategies = {}
    for ticker_name, ticker_conf in config.tickers.items():
        strategies[ticker_name] = get_strategy(ticker_conf.strategy)

    # 6. Инициализировать execution manager
    execution = ExecutionManager(
        broker=broker,
        risk=risk,
        journal=journal,
        state=state,
        notifier=notifier,
        account_id=account_id,
        commission_pct=config.commission_pct,
        max_candles_timeout=config.max_candles_timeout,
        max_consecutive_sl=config.max_consecutive_sl,
    )

    # 7. Восстановить открытые позиции после перезапуска
    execution.recover()

    # 8. Уведомить о запуске
    mode = "sandbox" if config.broker.sandbox else "live"
    logger.info("Бот запущен. Режим: %s. Тикеров: %d", mode, len(config.tickers))
    if notifier:
        notifier.send(f"Бот запущен. Режим: {mode}. Тикеров: {len(config.tickers)}")

    # 9. Основной цикл
    while True:
        try:
            for ticker_name, ticker_conf in config.tickers.items():
                figi = ticker_conf.figi
                strategy = strategies[ticker_name]

                # Всегда включаем 30m для отсчёта таймаута позиции
                timeframes = list(dict.fromkeys(["30m"] + strategy.required_timeframes))
                try:
                    candles = feed.get_candles(
                        figi=figi,
                        timeframes=timeframes,
                        days=3,
                    )
                except Exception:
                    logger.exception("[MAIN] Error fetching data for %s", ticker_name)
                    continue

                if not candles:
                    continue

                # Текущая цена
                min_tf = strategy.required_timeframes[0]
                current_price = candles[min_tf].iloc[-1]["close"]

                if execution.has_position(figi):
                    last_candle_time = candles["30m"].index[-1].to_pydatetime()
                    execution.update(figi, current_price, last_candle_time)
                elif execution.is_ticker_blocked(ticker_name):
                    continue
                else:
                    setup = strategy.find_setup(candles)
                    if setup:
                        execution.open_position(ticker_name, figi, setup)

            time.sleep(config.poll_interval_sec)

        except KeyboardInterrupt:
            logger.info("Бот остановлен пользователем.")
            break
        except Exception as e:
            logger.exception("Ошибка в основном цикле: %s", e)
            if notifier:
                notifier.send(f"Ошибка: {e}")
            time.sleep(config.poll_interval_sec)


if __name__ == "__main__":
    main()
