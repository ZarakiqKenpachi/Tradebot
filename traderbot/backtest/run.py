"""
Точка входа бэктеста.
Запуск: py -3.12 -m traderbot.backtest.run
        py -3.12 -m traderbot.backtest.run --tickers SBER,GAZP --days 60
"""
import argparse
import logging
import os
import sys

from traderbot.broker.tbank import TBankBroker
from traderbot.config import load_config
from traderbot.data.feed import DataFeed
from traderbot.strategies.registry import get_strategy
from traderbot.backtest.engine import BacktestEngine
from traderbot.backtest.report import BacktestReport


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    # CLI аргументы
    parser = argparse.ArgumentParser(description="TraderBot Backtest")
    parser.add_argument("--config", default="config.yaml", help="Path to config file")
    parser.add_argument("--tickers", default=None, help="Comma-separated ticker list (default: all from config)")
    parser.add_argument("--days", type=int, default=None, help="Lookback days (default: from config)")
    args = parser.parse_args()

    # Загрузить конфиг
    config = load_config(args.config)
    days = args.days or config.backtest_days

    # Выбрать тикеры
    if args.tickers:
        selected = args.tickers.split(",")
        tickers = {k: v for k, v in config.tickers.items() if k in selected}
    else:
        tickers = config.tickers

    if not tickers:
        logger.error("No tickers selected")
        sys.exit(1)

    # Инициализировать брокер (sandbox для получения данных)
    broker = TBankBroker(config.broker.token, sandbox=True, app_name=config.broker.app_name)
    feed = DataFeed(broker)

    # Загрузить размеры лотов и свечи для всех тикеров
    logger.info("Loading data for %d tickers, %d days...", len(tickers), days)
    all_data = {}
    for ticker_name, ticker_conf in tickers.items():
        strategy = get_strategy(ticker_conf.strategy)
        logger.info("  Loading %s...", ticker_name)

        # Получить размер лота из API
        lot_size, _ = broker.get_instrument_info(ticker_conf.figi)
        if lot_size < 1:
            logger.warning("  Invalid lot_size=%d for %s, defaulting to 1", lot_size, ticker_name)
            lot_size = 1
        ticker_conf.lot_size = lot_size
        logger.info("  %s: lot_size=%d", ticker_name, lot_size)

        # Всегда загружаем 1m для точного времени входа/выхода в бэктесте
        timeframes = list(strategy.required_timeframes)
        if "1m" not in timeframes:
            timeframes = ["1m"] + timeframes
        data = feed.get_candles_history(
            figi=ticker_conf.figi,
            timeframes=timeframes,
            days=days,
        )
        if data:
            all_data[ticker_name] = data
        else:
            logger.warning("  No data for %s", ticker_name)

    if not all_data:
        logger.error("No data loaded")
        sys.exit(1)

    # Запустить симуляцию
    logger.info("Running backtest...")
    engine = BacktestEngine(config)
    results = engine.run(all_data)

    # Вывести отчёт
    report = BacktestReport(results, config.backtest_initial_balance)
    report.print_summary()
    report.export_csv(config.backtest_output_dir)

    logger.info("Done. Results in %s/", config.backtest_output_dir)


if __name__ == "__main__":
    main()
