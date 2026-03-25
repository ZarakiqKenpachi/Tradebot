"""
Backtest the ICT strategy against historical candle data.
Usage: py -3.12 backtest.py
"""

import os
from dataclasses import dataclass

import pandas as pd
from dotenv import load_dotenv
from t_tech.invest import CandleInterval

from bot.broker.tinkoff import TinkoffBroker
from bot.data.storage import MarketDataStorage
from bot.risk.manager import RiskManager
from bot.strategies.base import Signal
from bot.execution.manager import MAX_CANDLES_30M
from bot.strategies.ict import ICTStrategy

load_dotenv()

TICKERS = {
    "SBER": "BBG004730N88",
    "GAZP": "BBG004730RP0",
    "YDEX": "BBG1M2SHGLV3",   # post-restructuring Yandex — verify if no data
    "VTBR": "BBG004730ZJ9",
    "GMKN": "BBG004731489",
    "T":    "BBG00QPYJ5H0",   # T-Bank (ex-Tinkoff) — verify if no data
    "LKOH": "BBG004731032",
    "TATN": "BBG004RVFCY3",
    "ROSN": "BBG004731354",
    "NVTK": "BBG00475KKY8",
}

DAYS = 30
INITIAL_BALANCE = 100_000.0
RR_VALUES = [1.5, 2.0]


@dataclass
class Trade:
    ticker: str
    direction: str
    entry: float
    exit: float
    stop: float
    target: float
    pnl: float
    reason: str
    candles_held: int
    balance_after: float = 0.0
    entry_time: str = ""


def run():
    token = os.getenv("TINKOFF_TOKEN")
    broker = TinkoffBroker(token=token, sandbox=True)
    storage = MarketDataStorage()
    risk = RiskManager(risk_pct=0.01)

    ticker_data: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}

    for ticker, figi in TICKERS.items():
        print(f"Fetching {DAYS} days of 1m candles for {ticker} ({figi})...")
        try:
            df_1m = broker.get_all_candles(figi, CandleInterval.CANDLE_INTERVAL_1_MIN, days=DAYS)
            if df_1m.empty:
                print(f"  [SKIP] No data returned for {ticker}")
                continue
            df_30m = storage.resample(df_1m, "30min").iloc[:-1]
            df_1h  = storage.resample(df_1m, "1h").iloc[:-1]
            ticker_data[ticker] = (df_30m, df_1h)
            print(f"  {len(df_1m)} x 1m -> {len(df_30m)} x 30m | {len(df_1h)} x 1h")
        except Exception as e:
            print(f"  [SKIP] {ticker}: {e}")

    if not ticker_data:
        print("No data fetched. Exiting.")
        return

    print()
    for rr in RR_VALUES:
        _run_simulation(ticker_data, risk, rr)


def _run_simulation(
    ticker_data: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
    risk: RiskManager,
    rr: float,
):
    strategy = ICTStrategy(risk_reward=rr)
    all_trades: list[Trade] = []
    balance = INITIAL_BALANCE
    positions: dict[str, dict] = {}  # ticker -> {setup, qty, candles_held}

    # All MOEX stocks share the same trading hours — use the longest timeline
    master_30m = max(ticker_data.values(), key=lambda t: len(t[0]))[0]

    for i in range(MAX_CANDLES_30M + 1, len(master_30m)):
        current_time = master_30m.index[i]

        for ticker, (df_30m, df_1h) in ticker_data.items():
            df_30m_win = df_30m[df_30m.index <= current_time]
            df_1h_win  = df_1h[df_1h.index <= current_time]

            if df_30m_win.empty:
                continue

            # --- Manage open position ---
            if ticker in positions:
                pos = positions[ticker]
                pos["candles_held"] += 1
                price = df_30m_win.iloc[-1]["close"]
                setup = pos["setup"]
                reason = ""

                if pos["candles_held"] >= MAX_CANDLES_30M:
                    reason = "timeout"
                elif setup.direction == Signal.BUY:
                    if price <= setup.stop_price:
                        reason = "stop_loss"
                    elif price >= setup.target_price:
                        reason = "take_profit"
                else:
                    if price >= setup.stop_price:
                        reason = "stop_loss"
                    elif price <= setup.target_price:
                        reason = "take_profit"

                if reason:
                    pnl = (
                        (price - setup.entry_price) * pos["qty"]
                        if setup.direction == Signal.BUY
                        else (setup.entry_price - price) * pos["qty"]
                    )
                    balance += pnl
                    all_trades.append(Trade(
                        ticker=ticker,
                        direction=setup.direction.value,
                        entry=setup.entry_price,
                        exit=price,
                        stop=setup.stop_price,
                        target=setup.target_price,
                        pnl=round(pnl, 2),
                        reason=reason,
                        candles_held=pos["candles_held"],
                        balance_after=round(balance, 2),
                        entry_time=pos["entry_time"].tz_convert("Europe/Moscow").strftime("%Y-%m-%d %H:%M"),
                    ))
                    del positions[ticker]

            # --- Look for new setup ---
            if ticker not in positions and len(df_1h_win) >= 5:
                setup = strategy.find_setup(df_1h_win, df_30m_win)
                if setup:
                    qty = risk.position_size(balance, setup.entry_price, setup.stop_price)
                    if qty >= 1:
                        positions[ticker] = {"setup": setup, "qty": qty, "candles_held": 0, "entry_time": current_time}

    _print_results(all_trades, balance, rr)


def _print_results(trades: list[Trade], balance: float, rr: float):
    W = 58
    tickers = sorted(set(t.ticker for t in trades))

    header = f"  RR 1:{rr} | {DAYS} days | {len(tickers)} tickers"
    print("=" * W)
    print(header)
    print("=" * W)

    if not trades:
        print("  No trades found.\n")
        return

    wins      = [t for t in trades if t.pnl > 0]
    losses    = [t for t in trades if t.pnl <= 0]
    tp        = [t for t in trades if t.reason == "take_profit"]
    sl        = [t for t in trades if t.reason == "stop_loss"]
    to        = [t for t in trades if t.reason == "timeout"]
    total_pnl = sum(t.pnl for t in trades)
    ret_pct   = (balance - INITIAL_BALANCE) / INITIAL_BALANCE * 100

    # -- BOARD 1: Overall summary ---------------------------------
    print()
    print("  +--- BOARD 1: OVERALL ----------------------------------+")
    print(f"  |  Starting balance : {INITIAL_BALANCE:>10.0f} RUB                   |")
    print(f"  |  Final balance    : {balance:>10.2f} RUB                   |")
    pnl_sign = "+" if total_pnl >= 0 else ""
    ret_sign  = "+" if ret_pct  >= 0 else ""
    print(f"  |  Total P&L        : {pnl_sign}{total_pnl:>10.2f} RUB                   |")
    print(f"  |  Return           : {ret_sign}{ret_pct:>9.2f}%                     |")
    print(f"  |  Total trades     : {len(trades):>10}                         |")
    print(f"  |  Wins / Losses    : {len(wins):>4} / {len(losses):<4}  "
          f"({len(wins)/len(trades)*100:.1f}% win rate)       |")
    print(f"  |  TP / SL / TO     : {len(tp):>4} / {len(sl):<4} / {len(to):<4}                  |")
    avg_win  = sum(t.pnl for t in wins)  / len(wins)  if wins   else 0
    avg_loss = sum(t.pnl for t in losses)/ len(losses) if losses else 0
    print(f"  |  Avg win          : {avg_win:>+10.2f} RUB                   |")
    print(f"  |  Avg loss         : {avg_loss:>+10.2f} RUB                   |")
    print("  +-------------------------------------------------------+")

    # -- BOARD 2: Per-ticker breakdown ----------------------------
    print()
    print("  +--- BOARD 2: BY TICKER --------------------------------+")
    print(f"  |  {'TICKER':<6}  {'TRADES':>6}  {'W':>4}  {'L':>4}  {'WIN%':>5}  {'P&L':>14}  |")
    print(f"  |  {'-'*6}  {'-'*6}  {'-'*4}  {'-'*4}  {'-'*5}  {'-'*14}  |")
    for tkr in tickers:
        tt  = [t for t in trades if t.ticker == tkr]
        tw  = [t for t in tt if t.pnl > 0]
        tl  = [t for t in tt if t.pnl <= 0]
        pnl = sum(t.pnl for t in tt)
        sign = "+" if pnl >= 0 else ""
        print(f"  |  {tkr:<6}  {len(tt):>6}  {len(tw):>4}  {len(tl):>4}  "
              f"{len(tw)/len(tt)*100:>4.0f}%  {sign}{pnl:>13.2f}  |")
    print("  +-------------------------------------------------------+")

    # ── Trade log ────────────────────────────────────────────────
    print()
    print(f"  {'#':<4} {'TICKER':<6} {'DIR':<5} {'ENTRY TIME':<17} {'ENTRY':>8} {'EXIT':>8} "
          f"{'P&L':>10} {'BALANCE':>12} {'REASON':<12} BARS")
    print(f"  {'-'*4} {'-'*6} {'-'*5} {'-'*17} {'-'*8} {'-'*8} "
          f"{'-'*10} {'-'*12} {'-'*12} {'-'*4}")
    for n, t in enumerate(trades, 1):
        sign = "+" if t.pnl >= 0 else ""
        print(f"  {n:<4} {t.ticker:<6} {t.direction:<5} {t.entry_time:<17} {t.entry:>8.2f} {t.exit:>8.2f} "
              f"{sign}{t.pnl:>9.2f} {t.balance_after:>12.2f} {t.reason:<12} {t.candles_held}")
    print()


if __name__ == "__main__":
    run()
