"""
Сравнение стратегий по кэшированным данным.
1) Загружает свечи один раз через API
2) Прогоняет все стратегии по одним и тем же данным
3) Выводит матрицу P&L и лучший микс

Запуск: py -3.12 -m traderbot.backtest.compare --days 90
"""
import argparse
import logging
import sys

from traderbot.broker.tbank import TBankBroker
from traderbot.config import load_config
from traderbot.data.feed import DataFeed
from traderbot.strategies.registry import get_strategy
from traderbot.backtest.engine import BacktestEngine
from traderbot.backtest.report import BacktestReport


STRATEGY_VARIANTS = [
    "ict",
    "ict_v2_sw4_rr2",
    "ict_v2_sw4_rr35",
    "ict_v2_sw10_rr2",
    "ict_v2_sw10_rr35",
]

NAMES = {
    "ict": "V1 sw15 RR2",
    "ict_v2_sw4_rr2": "sw4 RR2",
    "ict_v2_sw4_rr35": "sw4 RR3.5",
    "ict_v2_sw10_rr2": "sw10 RR2",
    "ict_v2_sw10_rr35": "sw10 RR3.5",
}


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    setup_logging()
    logger = logging.getLogger(__name__)

    parser = argparse.ArgumentParser(description="Compare strategies")
    parser.add_argument("--config", default="traderbot/config.yaml")
    parser.add_argument("--tickers", default=None)
    parser.add_argument("--days", type=int, default=90)
    args = parser.parse_args()

    config = load_config(args.config)
    days = args.days

    if args.tickers:
        selected = args.tickers.split(",")
        tickers = {k: v for k, v in config.tickers.items() if k in selected}
    else:
        tickers = config.tickers

    # === Шаг 1: загрузить данные ОДИН раз ===
    broker = TBankBroker(config.broker.token, sandbox=True, app_name=config.broker.app_name)
    feed = DataFeed(broker)

    # Собираем все нужные таймфреймы из всех стратегий
    all_timeframes = set(["1m"])
    for strat_name in STRATEGY_VARIANTS:
        s = get_strategy(strat_name)
        all_timeframes.update(s.required_timeframes)
    all_timeframes = sorted(all_timeframes)

    logger.info("Loading data for %d tickers, %d days, timeframes=%s...",
                len(tickers), days, all_timeframes)

    all_data = {}
    for ticker_name, ticker_conf in tickers.items():
        logger.info("  Loading %s...", ticker_name)
        data = feed.get_candles_history(
            figi=ticker_conf.figi,
            timeframes=all_timeframes,
            days=days,
        )
        if data:
            all_data[ticker_name] = data
        else:
            logger.warning("  No data for %s", ticker_name)

    if not all_data:
        logger.error("No data loaded")
        sys.exit(1)

    logger.info("Data loaded. Running %d strategy variants...", len(STRATEGY_VARIANTS))

    # === Шаг 2: прогнать все стратегии по одним данным ===
    # {strat_name: {ticker: {pnl, tr, w, tp, sl, to, pf}}}
    results = {}

    for strat_name in STRATEGY_VARIANTS:
        logger.info("  Strategy: %s", NAMES[strat_name])

        # Подменяем стратегию в конфиге для каждого тикера
        for tc in config.tickers.values():
            tc.strategy = strat_name

        engine = BacktestEngine(config)
        trades = engine.run(all_data)

        # Собираем статистику по тикерам
        strat_data = {}
        for ticker_name in tickers:
            ticker_trades = [t for t in trades if t.ticker == ticker_name]
            wins = [t for t in ticker_trades if t.pnl > 0]
            losses = [t for t in ticker_trades if t.pnl <= 0]
            gross_w = sum(t.pnl for t in wins)
            gross_l = abs(sum(t.pnl for t in losses))
            strat_data[ticker_name] = {
                "pnl": sum(t.pnl for t in ticker_trades),
                "tr": len(ticker_trades),
                "w": len(wins),
                "tp": sum(1 for t in ticker_trades if t.exit_reason == "take_profit"),
                "sl": sum(1 for t in ticker_trades if t.exit_reason == "stop_loss"),
                "to": sum(1 for t in ticker_trades if t.exit_reason in ("timeout", "end_of_data")),
                "pf": gross_w / gross_l if gross_l > 0 else 0,
            }
        results[strat_name] = strat_data

    # === Шаг 3: вывести результаты ===
    ticker_names = list(tickers.keys())

    print()
    print("=" * 105)
    print(f"FULL MATRIX ({days} days): P&L by ticker x strategy")
    print("=" * 105)
    header = f"{'Ticker':<6}"
    for s in STRATEGY_VARIANTS:
        header += f" | {NAMES[s]:>16}"
    header += " | BEST"
    print(header)
    print("-" * 105)

    best_per_ticker = {}
    for t in ticker_names:
        line = f"{t:<6}"
        best_strat = None
        best_pnl = -999999
        for s in STRATEGY_VARIANTS:
            d = results[s][t]
            pnl = d["pnl"]
            wr = d["w"] / d["tr"] * 100 if d["tr"] else 0
            line += f" | {pnl:>+8,.0f} {wr:>3.0f}% {d['tr']:>3}t"
            if pnl > best_pnl:
                best_pnl = pnl
                best_strat = s
        line += f" | {NAMES[best_strat]}"
        best_per_ticker[t] = best_strat
        print(line)

    print("=" * 105)

    # Best assignment
    print(f"\nBEST STRATEGY PER TICKER ({days} days):")
    print("-" * 85)
    print(f"  {'Ticker':<6} {'Strategy':<14} {'P&L':>10} {'Tr':>4} {'TP':>4} {'SL':>4} {'TO':>4} {'WR':>5} {'PF':>5}")
    print("-" * 85)
    total_pnl = 0
    total_tr = 0
    total_w = 0
    for t in ticker_names:
        s = best_per_ticker[t]
        d = results[s][t]
        wr = d["w"] / d["tr"] * 100 if d["tr"] else 0
        print(f"  {t:<6} {NAMES[s]:<14} {d['pnl']:>+10,.0f} {d['tr']:>4} {d['tp']:>4} {d['sl']:>4} {d['to']:>4} {wr:>4.0f}% {d['pf']:>5.2f}")
        total_pnl += d["pnl"]
        total_tr += d["tr"]
        total_w += d["w"]

    print("-" * 85)
    total_wr = total_w / total_tr * 100 if total_tr else 0
    print(f"  {'TOTAL':<6} {'BEST MIX':<14} {total_pnl:>+10,.0f} {total_tr:>4} {'':>4} {'':>4} {'':>4} {total_wr:>4.0f}%")

    # Compare with uniform
    print(f"\nCOMPARISON: best-per-ticker vs uniform")
    print("-" * 60)
    for s in STRATEGY_VARIANTS:
        tp = sum(results[s][t]["pnl"] for t in ticker_names)
        tr = sum(results[s][t]["tr"] for t in ticker_names)
        w = sum(results[s][t]["w"] for t in ticker_names)
        wr = w / tr * 100 if tr else 0
        print(f"  {NAMES[s]:<16} P&L: {tp:>+10,.0f}  {w}/{tr} trades  WR:{wr:.0f}%")
    print(f"  {'BEST MIX':<16} P&L: {total_pnl:>+10,.0f}  {total_w}/{total_tr} trades  WR:{total_wr:.0f}%")


if __name__ == "__main__":
    main()
