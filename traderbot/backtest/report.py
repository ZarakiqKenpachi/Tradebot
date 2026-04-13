import csv
import logging
import os
from datetime import datetime

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.ticker as mticker

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

    def plot_results(self, output_dir: str) -> None:
        """Построить графики результатов бэктеста и сохранить как PNG."""
        if not self.trades:
            return

        os.makedirs(output_dir, exist_ok=True)

        sorted_trades = sorted(self.trades, key=lambda t: t.exit_time)

        # ── Данные для графиков ──
        dates = [t.exit_time for t in sorted_trades]
        pnls = [t.pnl for t in sorted_trades]

        # Equity curve
        equity = [self.initial_balance]
        for p in pnls:
            equity.append(equity[-1] + p)
        equity_dates = [sorted_trades[0].entry_time] + dates

        # Cumulative P&L
        cum_pnl = []
        running = 0.0
        for p in pnls:
            running += p
            cum_pnl.append(running)

        # Drawdown
        peak = self.initial_balance
        drawdowns = []
        for eq in equity:
            if eq > peak:
                peak = eq
            dd_pct = ((peak - eq) / peak) * 100
            drawdowns.append(dd_pct)

        # P&L по тикерам
        ticker_pnl: dict[str, float] = {}
        ticker_wins: dict[str, int] = {}
        ticker_total: dict[str, int] = {}
        for t in sorted_trades:
            ticker_pnl[t.ticker] = ticker_pnl.get(t.ticker, 0) + t.pnl
            ticker_total[t.ticker] = ticker_total.get(t.ticker, 0) + 1
            if t.pnl > 0:
                ticker_wins[t.ticker] = ticker_wins.get(t.ticker, 0) + 1

        # ── Фигура: 2x2 графика ──
        fig, axes = plt.subplots(2, 2, figsize=(16, 10))
        fig.suptitle("Backtest Results", fontsize=16, fontweight="bold")
        plt.subplots_adjust(hspace=0.35, wspace=0.3)

        # 1. Equity Curve
        ax1 = axes[0][0]
        ax1.plot(equity_dates, equity, color="#2196F3", linewidth=1.5)
        ax1.axhline(y=self.initial_balance, color="gray", linestyle="--", alpha=0.5)
        ax1.fill_between(equity_dates, self.initial_balance, equity,
                         where=[e >= self.initial_balance for e in equity],
                         alpha=0.15, color="green")
        ax1.fill_between(equity_dates, self.initial_balance, equity,
                         where=[e < self.initial_balance for e in equity],
                         alpha=0.15, color="red")
        ax1.set_title("Equity Curve")
        ax1.set_ylabel("Balance (RUB)")
        ax1.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        ax1.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
        ax1.grid(True, alpha=0.3)

        # 2. Drawdown
        ax2 = axes[0][1]
        ax2.fill_between(equity_dates, 0, drawdowns, color="#F44336", alpha=0.4)
        ax2.plot(equity_dates, drawdowns, color="#F44336", linewidth=1)
        ax2.set_title("Drawdown")
        ax2.set_ylabel("Drawdown (%)")
        ax2.invert_yaxis()
        ax2.xaxis.set_major_formatter(mdates.DateFormatter("%d.%m"))
        ax2.grid(True, alpha=0.3)

        # Статистика в тексте на графике drawdown
        max_dd = max(drawdowns)
        total_pnl = sum(pnls)
        win_count = sum(1 for p in pnls if p > 0)
        win_rate = win_count / len(pnls) * 100
        avg_win = sum(p for p in pnls if p > 0) / win_count if win_count else 0
        loss_count = len(pnls) - win_count
        avg_loss = sum(p for p in pnls if p <= 0) / loss_count if loss_count else 0
        profit_factor = abs(sum(p for p in pnls if p > 0) / sum(p for p in pnls if p < 0)) if sum(p for p in pnls if p < 0) != 0 else float("inf")

        stats_text = (
            f"P&L: {total_pnl:+,.0f} RUB\n"
            f"Return: {total_pnl / self.initial_balance * 100:+.1f}%\n"
            f"Max DD: {max_dd:.1f}%\n"
            f"Trades: {len(pnls)}\n"
            f"Win Rate: {win_rate:.0f}%\n"
            f"Avg Win: {avg_win:+,.0f}\n"
            f"Avg Loss: {avg_loss:+,.0f}\n"
            f"Profit Factor: {profit_factor:.2f}"
        )
        ax2.text(0.98, 0.98, stats_text, transform=ax2.transAxes,
                 fontsize=9, verticalalignment="top", horizontalalignment="right",
                 bbox=dict(boxstyle="round,pad=0.4", facecolor="white", alpha=0.8),
                 fontfamily="monospace")

        # 3. P&L по сделкам (bar chart)
        ax3 = axes[1][0]
        colors = ["#4CAF50" if p > 0 else "#F44336" for p in pnls]
        ax3.bar(range(len(pnls)), pnls, color=colors, width=1.0, edgecolor="none")
        ax3.axhline(y=0, color="gray", linewidth=0.5)
        ax3.set_title("P&L per Trade")
        ax3.set_xlabel("Trade #")
        ax3.set_ylabel("P&L (RUB)")
        ax3.yaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
        ax3.grid(True, alpha=0.3, axis="y")

        # 4. P&L по тикерам (horizontal bar)
        ax4 = axes[1][1]
        tickers_sorted = sorted(ticker_pnl.keys(), key=lambda k: ticker_pnl[k], reverse=True)
        vals = [ticker_pnl[t] for t in tickers_sorted]
        bar_colors = ["#4CAF50" if v > 0 else "#F44336" for v in vals]
        labels = [f"{t} ({ticker_wins.get(t, 0)}W/{ticker_total[t] - ticker_wins.get(t, 0)}L)"
                  for t in tickers_sorted]
        bars = ax4.barh(labels, vals, color=bar_colors, edgecolor="none")
        ax4.axvline(x=0, color="gray", linewidth=0.5)
        ax4.set_title("P&L by Ticker")
        ax4.set_xlabel("P&L (RUB)")
        ax4.xaxis.set_major_formatter(mticker.FuncFormatter(lambda x, _: f"{x:,.0f}"))
        ax4.grid(True, alpha=0.3, axis="x")
        # Значения на барах
        for bar, val in zip(bars, vals):
            ax4.text(val + (max(abs(v) for v in vals) * 0.02 * (1 if val >= 0 else -1)),
                     bar.get_y() + bar.get_height() / 2,
                     f"{val:+,.0f}", va="center", fontsize=8)

        # Сохранить
        path = os.path.join(output_dir, "backtest_results.png")
        fig.savefig(path, dpi=150, bbox_inches="tight", facecolor="white")
        plt.close(fig)
        logger.info("[REPORT] Chart saved: %s", path)

        # Показать график
        try:
            plt.figure(fig.number)
        except Exception:
            pass
        fig2 = plt.figure(figsize=(16, 10))
        img = plt.imread(path)
        plt.imshow(img)
        plt.axis("off")
        plt.tight_layout(pad=0)
        plt.show()
        plt.close(fig2)
