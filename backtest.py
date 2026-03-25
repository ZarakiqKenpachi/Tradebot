"""
Backtest the ICT strategy against historical candle data.
Usage:
  py -3.12 backtest.py           # API data, last 30 days
  py -3.12 backtest.py truedata  # TrueData files, SBER only, up to 2026-02-24
"""

import os
import sys
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
    "GAZP": "BBG004730RP0",
    "GMKN": "BBG004731489",
    "VTBR": "BBG004730ZJ9",
    "SBER": "BBG004730N88",
    "ROSN": "BBG004731354",
    "NVTK": "BBG00475KKY8",
}

TRUEDATA_30M = "truedata/SBER_251224_260324 (4).txt"
TRUEDATA_1H  = "truedata/SBER_251224_260324 (3).txt"
TRUEDATA_END = "2026-02-24"   # inclusive upper bound

DAYS = 30
INITIAL_BALANCE = 100_000.0
COMMISSION_PCT = 0.00004  # 0.004% per side
RR_VALUES = [2.0]


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
            df_30m = _filter_moex_hours(storage.resample(df_1m, "30min").iloc[:-1])
            df_1h  = _filter_moex_hours(storage.resample(df_1m, "1h").iloc[:-1])
            ticker_data[ticker] = (df_30m, df_1h)
            print(f"  {len(df_1m)} x 1m -> {len(df_30m)} x 30m | {len(df_1h)} x 1h")
        except Exception as e:
            print(f"  [SKIP] {ticker}: {e}")

    if not ticker_data:
        print("No data fetched. Exiting.")
        return

    print()
    all_trades_combined: list[tuple[float, Trade]] = []
    for rr in RR_VALUES:
        trades = _run_simulation(ticker_data, risk, rr)
        all_trades_combined.extend((rr, t) for t in trades)

    _export_trades(all_trades_combined, ["GAZP", "GMKN"])


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

                effective_sl = pos["sl_price"]

                if pos["candles_held"] >= MAX_CANDLES_30M:
                    reason = "timeout"
                elif setup.direction == Signal.BUY:
                    if price <= effective_sl:
                        reason = "stop_loss"
                    elif price >= setup.target_price:
                        reason = "take_profit"
                else:
                    if price >= effective_sl:
                        reason = "stop_loss"
                    elif price <= setup.target_price:
                        reason = "take_profit"

                if reason:
                    exit_price = price
                    if reason == "stop_loss":
                        exit_price = pos["sl_price"]
                    elif reason == "take_profit":
                        exit_price = setup.target_price
                    raw_pnl = (
                        (exit_price - setup.entry_price) * pos["qty"]
                        if setup.direction == Signal.BUY
                        else (setup.entry_price - exit_price) * pos["qty"]
                    )
                    commission = (setup.entry_price + exit_price) * pos["qty"] * COMMISSION_PCT
                    pnl = raw_pnl - commission
                    balance += pnl
                    all_trades.append(Trade(
                        ticker=ticker,
                        direction=setup.direction.value,
                        entry=setup.entry_price,
                        exit=exit_price,
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
                        positions[ticker] = {
                            "setup": setup,
                            "qty": qty,
                            "candles_held": 0,
                            "entry_time": current_time,
                            "sl_price": setup.stop_price,
                        }

    _print_results(all_trades, balance, rr)
    return all_trades


def _export_trades(all_trades: list[tuple[float, Trade]], tickers: list[str]):
    for ticker in tickers:
        rows = [
            {
                "rr":           rr,
                "entry_time":   t.entry_time,
                "direction":    t.direction,
                "entry":        t.entry,
                "exit":         t.exit,
                "stop":         t.stop,
                "target":       t.target,
                "qty":          None,
                "pnl":          t.pnl,
                "balance_after": t.balance_after,
                "reason":       t.reason,
                "bars_held":    t.candles_held,
            }
            for rr, t in all_trades
            if t.ticker == ticker
        ]
        if not rows:
            print(f"[EXPORT] No trades found for {ticker}, skipping.")
            continue
        df = pd.DataFrame(rows)
        path = f"{ticker}_trades.csv"
        df.to_csv(path, index=False)
        print(f"[EXPORT] {ticker}: {len(rows)} rows -> {path}")


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


_storage = MarketDataStorage()


def _filter_moex_hours(df: pd.DataFrame) -> pd.DataFrame:
    return _storage.filter_moex_hours(df)


def _load_truedata(path: str, end: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip("<>").lower() for c in df.columns]
    df["time"] = pd.to_datetime(
        df["date"].astype(str) + df["time"].astype(str).str.zfill(6),
        format="%Y%m%d%H%M%S",
    )
    df["time"] = df["time"].dt.tz_localize("Europe/Moscow").dt.tz_convert("UTC")
    df = df.set_index("time").sort_index()
    df = df.rename(columns={"vol": "volume"})[["open", "high", "low", "close", "volume"]]
    end_utc = pd.Timestamp(end, tz="Europe/Moscow").tz_convert("UTC") + pd.Timedelta(days=1)
    return df[df.index < end_utc]


def run_truedata():
    risk = RiskManager(risk_pct=0.01)

    df_30m = _filter_moex_hours(_load_truedata(TRUEDATA_30M, TRUEDATA_END))
    df_1h  = _filter_moex_hours(_load_truedata(TRUEDATA_1H,  TRUEDATA_END))

    # Use last 30 days of available data
    cutoff = df_30m.index.max() - pd.Timedelta(days=30)
    df_30m = df_30m[df_30m.index > cutoff]
    df_1h  = df_1h[df_1h.index > cutoff]

    print(f"TrueData SBER: {len(df_30m)} x 30m | {len(df_1h)} x 1h")
    print(f"  Period: {df_30m.index.min().tz_convert('Europe/Moscow').date()} "
          f"to {df_30m.index.max().tz_convert('Europe/Moscow').date()}")
    print()

    ticker_data = {"SBER": (df_30m, df_1h)}

    for rr in RR_VALUES:
        _run_simulation(ticker_data, risk, rr)


def _run_simulation_return(
    ticker_data: dict[str, tuple[pd.DataFrame, pd.DataFrame]],
    risk: RiskManager,
    rr: float,
) -> list[Trade]:
    """Same as _run_simulation but returns trades instead of printing."""
    strategy = ICTStrategy(risk_reward=rr)
    all_trades: list[Trade] = []
    balance = INITIAL_BALANCE
    positions: dict[str, dict] = {}

    master_30m = max(ticker_data.values(), key=lambda t: len(t[0]))[0]

    for i in range(MAX_CANDLES_30M + 1, len(master_30m)):
        current_time = master_30m.index[i]
        for ticker, (df_30m, df_1h) in ticker_data.items():
            df_30m_win = df_30m[df_30m.index <= current_time]
            df_1h_win  = df_1h[df_1h.index <= current_time]
            if df_30m_win.empty:
                continue
            if ticker in positions:
                pos = positions[ticker]
                pos["candles_held"] += 1
                price = df_30m_win.iloc[-1]["close"]
                setup = pos["setup"]
                reason = ""
                effective_sl = pos["sl_price"]
                if pos["candles_held"] >= MAX_CANDLES_30M:
                    reason = "timeout"
                elif setup.direction == Signal.BUY:
                    if price <= effective_sl:
                        reason = "stop_loss"
                    elif price >= setup.target_price:
                        reason = "take_profit"
                else:
                    if price >= effective_sl:
                        reason = "stop_loss"
                    elif price <= setup.target_price:
                        reason = "take_profit"
                if reason:
                    exit_price = price
                    if reason == "stop_loss":
                        exit_price = pos["sl_price"]
                    elif reason == "take_profit":
                        exit_price = setup.target_price
                    raw_pnl = (
                        (exit_price - setup.entry_price) * pos["qty"]
                        if setup.direction == Signal.BUY
                        else (setup.entry_price - exit_price) * pos["qty"]
                    )
                    commission = (setup.entry_price + exit_price) * pos["qty"] * COMMISSION_PCT
                    pnl = raw_pnl - commission
                    balance += pnl
                    all_trades.append(Trade(
                        ticker=ticker,
                        direction=setup.direction.value,
                        entry=setup.entry_price,
                        exit=exit_price,
                        stop=setup.stop_price,
                        target=setup.target_price,
                        pnl=round(pnl, 2),
                        reason=reason,
                        candles_held=pos["candles_held"],
                        balance_after=round(balance, 2),
                        entry_time=pos["entry_time"].tz_convert("Europe/Moscow").strftime("%Y-%m-%d %H:%M"),
                    ))
                    del positions[ticker]
            if ticker not in positions and len(df_1h_win) >= 5:
                setup = strategy.find_setup(df_1h_win, df_30m_win)
                if setup:
                    qty = risk.position_size(balance, setup.entry_price, setup.stop_price)
                    if qty >= 1:
                        positions[ticker] = {
                            "setup": setup,
                            "qty": qty,
                            "candles_held": 0,
                            "entry_time": current_time,
                            "sl_price": setup.stop_price,
                        }
    return all_trades


def run_compare():
    """Run SBER backtest on the same period using both API and TrueData, then compare."""
    token = os.getenv("TINKOFF_TOKEN")
    broker = TinkoffBroker(token=token, sandbox=True)
    storage = MarketDataStorage()
    risk = RiskManager(risk_pct=0.01)

    # --- API data (filtered to MOEX regular hours) ---
    print("=== Loading API data (SBER, 30 days) ===")
    df_1m_api = broker.get_all_candles(SBER_FIGI, CandleInterval.CANDLE_INTERVAL_1_MIN, days=DAYS)
    api_30m = _filter_moex_hours(storage.resample(df_1m_api, "30min").iloc[:-1])
    api_1h  = _filter_moex_hours(storage.resample(df_1m_api, "1h").iloc[:-1])
    print(f"  API: {len(api_30m)} x 30m | {len(api_1h)} x 1h")

    # --- TrueData (same period, same filters) ---
    print("\n=== Loading TrueData (SBER, same period) ===")
    td_30m = _filter_moex_hours(_load_truedata(TRUEDATA_30M, "2026-03-24"))
    td_1h  = _filter_moex_hours(_load_truedata(TRUEDATA_1H,  "2026-03-24"))
    # Trim to the API range
    api_start = api_30m.index.min()
    td_30m = td_30m[td_30m.index >= api_start]
    td_1h  = td_1h[td_1h.index >= api_start]
    print(f"  TD:  {len(td_30m)} x 30m | {len(td_1h)} x 1h")
    print(f"  Period: {api_start.tz_convert('Europe/Moscow').date()} "
          f"to {api_30m.index.max().tz_convert('Europe/Moscow').date()}")

    rr = 3.5
    print(f"\n=== Backtest RR 1:{rr} ===\n")

    api_trades = _run_simulation_return({"SBER": (api_30m, api_1h)}, risk, rr)
    td_trades  = _run_simulation_return({"SBER": (td_30m, td_1h)}, risk, rr)

    # --- Side-by-side trade log ---
    max_len = max(len(api_trades), len(td_trades))
    print(f"  API: {len(api_trades)} trades | TrueData: {len(td_trades)} trades\n")
    print(f"  {'#':<3} {'ENTRY TIME':<17} {'DIR':<5} | {'API ENTRY':>9} {'API EXIT':>9} {'API PNL':>10} "
          f"| {'TD ENTRY':>9} {'TD EXIT':>9} {'TD PNL':>10} | {'dPNL':>8} {'MATCH':>5}")
    print(f"  {'-'*3} {'-'*17} {'-'*5} + {'-'*9} {'-'*9} {'-'*10} "
          f"+ {'-'*9} {'-'*9} {'-'*10} + {'-'*8} {'-'*5}")

    for i in range(max_len):
        a = api_trades[i] if i < len(api_trades) else None
        t = td_trades[i]  if i < len(td_trades) else None
        if a and t and a.entry_time == t.entry_time:
            dpnl = a.pnl - t.pnl
            match = "OK" if abs(dpnl) < 1.0 else "DIFF"
            sign = "+" if dpnl >= 0 else ""
            print(f"  {i+1:<3} {a.entry_time:<17} {a.direction:<5} | "
                  f"{a.entry:>9.2f} {a.exit:>9.2f} {a.pnl:>+10.2f} | "
                  f"{t.entry:>9.2f} {t.exit:>9.2f} {t.pnl:>+10.2f} | "
                  f"{sign}{dpnl:>7.2f} {match:>5}")
        elif a and t:
            print(f"  {i+1:<3} MISMATCH: API={a.entry_time} {a.direction} vs TD={t.entry_time} {t.direction}")
        elif a:
            print(f"  {i+1:<3} {a.entry_time:<17} {a.direction:<5} | "
                  f"{a.entry:>9.2f} {a.exit:>9.2f} {a.pnl:>+10.2f} | {'--- only in API':>32} |")
        else:
            print(f"  {i+1:<3} {t.entry_time:<17} {t.direction:<5} | {'--- only in TD':>32} | "
                  f"{t.entry:>9.2f} {t.exit:>9.2f} {t.pnl:>+10.2f} |")

    api_total = sum(t.pnl for t in api_trades)
    td_total  = sum(t.pnl for t in td_trades)
    print(f"\n  Total P&L:  API = {api_total:>+.2f}  |  TD = {td_total:>+.2f}  |  diff = {api_total - td_total:>+.2f}")
    print()


SBER_FIGI = "BBG004730N88"


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "truedata":
        run_truedata()
    elif len(sys.argv) > 1 and sys.argv[1] == "compare":
        run_compare()
    else:
        run()
