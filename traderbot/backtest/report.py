import csv
import logging
import os

from traderbot.types import TradeRecord

logger = logging.getLogger(__name__)


class BacktestReport:
    """Генерация отчётов и статистики бэктеста."""

    def __init__(self, trades: list[TradeRecord], initial_balance: float = 100000.0):
        self.trades = trades
        self.initial_balance = initial_balance

    def print_summary(self) -> None:
        if not self.trades:
            print("Нет сделок для отчёта.")
            return

        total_pnl = sum(t.pnl for t in self.trades)
        total_commission = sum(t.commission for t in self.trades)
        final_balance = self.initial_balance + total_pnl
        return_pct = (total_pnl / self.initial_balance) * 100

        wins = [t for t in self.trades if t.pnl > 0]
        losses = [t for t in self.trades if t.pnl <= 0]
        win_rate = len(wins) / len(self.trades) * 100 if self.trades else 0

        # Разбивка по причинам закрытия
        reasons = {}
        for t in self.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1

        print("=" * 60)
        print("BACKTEST SUMMARY")
        print("=" * 60)
        print(f"Начальный баланс:  {self.initial_balance:>12,.2f} RUB")
        print(f"Конечный баланс:   {final_balance:>12,.2f} RUB")
        print(f"P&L:               {total_pnl:>+12,.2f} RUB")
        print(f"Комиссия:          {total_commission:>12,.2f} RUB")
        print(f"Доходность:        {return_pct:>+11.2f}%")
        print(f"Всего сделок:      {len(self.trades):>12}")
        print(f"Побед:             {len(wins):>12}")
        print(f"Поражений:         {len(losses):>12}")
        print(f"Win rate:          {win_rate:>11.1f}%")
        print()

        # По причинам закрытия
        print("Причины закрытия:")
        for reason, count in sorted(reasons.items()):
            print(f"  {reason:<20} {count}")
        print()

        # По тикерам
        tickers = {}
        for t in self.trades:
            if t.ticker not in tickers:
                tickers[t.ticker] = {"trades": 0, "wins": 0, "pnl": 0.0}
            tickers[t.ticker]["trades"] += 1
            tickers[t.ticker]["pnl"] += t.pnl
            if t.pnl > 0:
                tickers[t.ticker]["wins"] += 1

        print(f"{'Тикер':<8} {'Сделки':>7} {'W':>4} {'L':>4} {'Win%':>7} {'P&L':>12}")
        print("-" * 50)
        for ticker, stats in sorted(tickers.items()):
            l = stats["trades"] - stats["wins"]
            wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] else 0
            print(f"{ticker:<8} {stats['trades']:>7} {stats['wins']:>4} {l:>4} {wr:>6.1f}% {stats['pnl']:>+12,.2f}")
        print()

        # Топ-5 лучших и худших
        sorted_trades = sorted(self.trades, key=lambda t: t.pnl, reverse=True)
        print("Топ-5 лучших:")
        for t in sorted_trades[:5]:
            print(f"  {t.ticker} {t.direction.value} | P&L: {t.pnl:+,.2f} | {t.exit_reason}")
        print()
        print("Топ-5 худших:")
        for t in sorted_trades[-5:]:
            print(f"  {t.ticker} {t.direction.value} | P&L: {t.pnl:+,.2f} | {t.exit_reason}")
        print("=" * 60)

    def export_csv(self, output_dir: str) -> None:
        """Экспортировать CSV для каждого тикера."""
        os.makedirs(output_dir, exist_ok=True)

        # Группировать по тикерам
        by_ticker: dict[str, list[TradeRecord]] = {}
        for t in self.trades:
            by_ticker.setdefault(t.ticker, []).append(t)

        columns = [
            "entry_time", "exit_time", "direction", "entry_price", "exit_price",
            "stop_price", "target_price", "qty", "pnl", "commission",
            "exit_reason", "candles_held", "entry_reason",
        ]

        balance = self.initial_balance
        for ticker, ticker_trades in by_ticker.items():
            path = os.path.join(output_dir, f"{ticker}_trades.csv")
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow(columns + ["balance_after"])
                for t in ticker_trades:
                    balance += t.pnl
                    writer.writerow([
                        t.entry_time.isoformat(),
                        t.exit_time.isoformat(),
                        t.direction.value,
                        t.entry_price,
                        t.exit_price,
                        t.stop_price,
                        t.target_price,
                        t.qty,
                        round(t.pnl, 2),
                        round(t.commission, 2),
                        t.exit_reason,
                        t.candles_held,
                        t.entry_reason,
                        round(balance, 2),
                    ])
            logger.info("[REPORT] Exported %s (%d trades)", path, len(ticker_trades))

    def export_trade_log(self, output_dir: str) -> None:
        """Единый лог всех сделок по всем активам, отсортированный по времени.

        Файл trade_log.csv перезаписывается при каждом запуске бэктеста.
        Используется для анализа и оптимизации торговой стратегии.
        """
        if not self.trades:
            return

        os.makedirs(output_dir, exist_ok=True)
        path = os.path.join(output_dir, "trade_log.csv")

        sorted_trades = sorted(self.trades, key=lambda t: t.entry_time)

        columns = [
            "ticker", "direction", "entry_time", "exit_time",
            "entry_price", "exit_price", "stop_price", "target_price",
            "qty", "pnl", "commission", "pnl_pct",
            "exit_reason", "entry_reason", "candles_held",
            "risk", "reward", "rr_actual",
            "balance_after",
        ]

        balance = self.initial_balance
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(columns)
            for t in sorted_trades:
                balance += t.pnl

                # Рассчитать risk/reward/RR
                if t.direction.value == "BUY":
                    risk = t.entry_price - t.stop_price
                    reward = t.target_price - t.entry_price
                else:
                    risk = t.stop_price - t.entry_price
                    reward = t.entry_price - t.target_price

                rr_actual = abs(t.pnl / (risk * t.qty)) if risk != 0 and t.qty != 0 else 0
                if t.pnl < 0:
                    rr_actual = -rr_actual

                pnl_pct = (t.pnl / self.initial_balance) * 100

                writer.writerow([
                    t.ticker,
                    t.direction.value,
                    t.entry_time.strftime("%Y-%m-%d %H:%M"),
                    t.exit_time.strftime("%Y-%m-%d %H:%M"),
                    t.entry_price,
                    t.exit_price,
                    t.stop_price,
                    t.target_price,
                    t.qty,
                    round(t.pnl, 2),
                    round(t.commission, 2),
                    round(pnl_pct, 3),
                    t.exit_reason,
                    t.entry_reason,
                    t.candles_held,
                    round(risk, 2),
                    round(reward, 2),
                    round(rr_actual, 2),
                    round(balance, 2),
                ])

        logger.info("[REPORT] Trade log: %s (%d trades)", path, len(sorted_trades))
