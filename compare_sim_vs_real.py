"""Compare chart simulation (Run All interleaved) vs real trades for last 2 days."""
import sqlite3
import logging
from datetime import timedelta

import pandas as pd
import yaml

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)

SIM_DAYS = 7


def load_real_trades() -> list[dict]:
    """Load unique real trades from last SIM_DAYS."""
    conn = sqlite3.connect("data/traderbot.db")
    conn.row_factory = sqlite3.Row
    rows = conn.execute(f"""
        SELECT ticker, direction, entry_price, exit_price, stop_price, target_price,
               qty, pnl, commission, entry_time, exit_time, exit_reason, candles_held
        FROM trades
        WHERE entry_time >= datetime('now', '-{SIM_DAYS} days')
        ORDER BY entry_time
    """).fetchall()
    conn.close()

    # Deduplicate by (ticker, direction, entry_price, exit_price, exit_reason)
    seen = set()
    unique = []
    for r in rows:
        key = (r["ticker"], r["direction"], r["entry_price"], round(r["exit_price"], 2), r["exit_reason"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(dict(r))
    return unique




def run_simulation() -> list[dict]:
    """Run interleaved simulation matching chart Run All logic."""
    from traderbot.broker.tbank import TBankBroker
    from traderbot.data.feed import DataFeed
    from traderbot.strategies.registry import STRATEGY_REGISTRY
    from traderbot.chart.strategy.runner import StrategyRunner, SimulationConfig

    # Load config
    with open("traderbot/config.yaml", "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)

    risk = cfg.get("risk", {})
    trading = cfg.get("trading", {})
    bt = cfg.get("backtest", {})

    # Connect to T-Bank
    TOKEN = "t.2ioHpwYVj4t12B_iRoqmfE3Rb4jkLIN1cSB7RDzBoAEJUjPo4tdkcjpiW_NGuzDKoj5aKVLbSc-syHejhbgIpg"
    broker = TBankBroker(token=TOKEN, sandbox=True, app_name="CompareScript-MD")
    feed = DataFeed(broker)

    # Ticker config
    tickers_cfg = cfg.get("tickers", {})
    runner = StrategyRunner()

    # Collect data for all tickers (same as app.py _on_run_all_tickers)
    ticker_states = {}
    for symbol, tc in tickers_cfg.items():
        strategy_name = tc.get("strategy", "")
        figi = tc.get("figi", "")
        if not strategy_name or not figi:
            continue

        strategy_cls = STRATEGY_REGISTRY.get(strategy_name)
        if not strategy_cls:
            logger.info("Strategy '%s' not found for %s, skipping", strategy_name, symbol)
            continue

        required = getattr(strategy_cls, "required_timeframes", [])
        all_tfs = list(set(required) | {"1m", "30m"})

        logger.info("Loading %s...", symbol)
        try:
            tbank_data = feed.get_candles_history(figi, all_tfs, days=SIM_DAYS + 15)
        except Exception as e:
            logger.info("  Failed: %s", e)
            continue

        if not tbank_data:
            continue

        candles = {}
        for tf in required:
            if tf in tbank_data and not tbank_data[tf].empty:
                candles[tf] = tbank_data[tf]
        scan_df = tbank_data.get("1m")

        if not candles:
            continue

        # DON'T trim candles — strategies need 15-day sliding window for warmup.
        # Only scan_df can be trimmed (it's only used for fill/exit scanning).
        # The interleaved runner uses _DAYS_WINDOW internally for strategy windows.

        lot_size, price_step = 1, 0.01
        try:
            lot_size, price_step = broker.get_instrument_info(figi)
        except Exception:
            pass

        dividend_dates = []
        try:
            divs = broker.get_dividends(figi, days_ahead=SIM_DAYS + 30)
            dividend_dates = [d["last_buy_date"] for d in divs if d["last_buy_date"]]
        except Exception:
            pass

        ticker_states[symbol] = {
            "strategy_name": strategy_name,
            "candles": candles,
            "scan_df": scan_df,
            "lot_size": lot_size,
            "price_step": price_step,
            "dividend_dates": dividend_dates,
            "required": required,
        }
        logger.info("  %s: loaded (%d candles in primary TF)", symbol, len(candles[required[0]]))

    if not ticker_states:
        logger.info("No data loaded!")
        return []

    # Run interleaved simulation
    sim_config = SimulationConfig(
        initial_balance=bt.get("initial_balance", 100_000.0),
        risk_pct=risk.get("risk_pct", 0.10),
        max_position_pct=risk.get("max_position_pct", 1.50),
        commission_pct=trading.get("commission_pct", 0.0004),
        slippage_pct=bt.get("slippage_pct", 0.0005),
        max_candles_timeout=trading.get("max_candles_timeout", 12),
        max_consecutive_sl=risk.get("max_consecutive_sl", 3),
        max_open_positions=risk.get("max_open_positions", 4),
    )

    sim_start = pd.Timestamp.now(tz="UTC") - timedelta(days=SIM_DAYS)

    logger.info("\nRunning interleaved simulation (max_open=%d, %d tickers)...",
                sim_config.max_open_positions, len(ticker_states))
    trades_by_ticker, final_balance = runner.run_interleaved(
        ticker_states, sim_config, sim_start=sim_start,
    )

    # Flatten
    all_trades = []
    for symbol, trades in trades_by_ticker.items():
        for t in trades:
            all_trades.append({
                "ticker": t.ticker,
                "direction": t.direction,
                "entry_price": t.entry_price,
                "exit_price": t.exit_price,
                "stop_price": t.stop_price,
                "target_price": t.target_price,
                "qty": t.qty,
                "pnl": t.pnl,
                "entry_time": t.entry_time,
                "exit_time": t.exit_time,
                "exit_reason": t.exit_reason,
                "candles_held": t.candles_held,
            })
    all_trades.sort(key=lambda t: t["entry_time"])
    return all_trades


def compare(real: list[dict], sim: list[dict]):
    """Print side-by-side comparison."""
    print("\n" + "=" * 120)
    print("REAL TRADES (last 2 days)")
    print("=" * 120)
    print(f"{'#':>2} {'Ticker':<6} {'Dir':<5} {'Entry':>8} {'Exit':>8} {'SL':>8} {'TP':>8} {'PnL':>10} {'Reason':<12} {'Bars':>4}  Entry time")
    print("-" * 120)
    for i, t in enumerate(real, 1):
        entry = t["entry_time"][:16].replace("T", " ")
        print(f"{i:>2} {t['ticker']:<6} {t['direction']:<5} {t['entry_price']:>8.2f} {t['exit_price']:>8.2f} "
              f"{t['stop_price']:>8.2f} {t['target_price']:>8.2f} {t['pnl']:>+10.2f} {t['exit_reason']:<12} "
              f"{t['candles_held']:>4}  {entry}")

    print(f"\n{'=' * 120}")
    print("SIMULATED TRADES (Run All interleaved, max_open_positions enforced)")
    print("=" * 120)
    print(f"{'#':>2} {'Ticker':<6} {'Dir':<5} {'Entry':>8} {'Exit':>8} {'SL':>8} {'TP':>8} {'PnL':>10} {'Reason':<12} {'Bars':>4}  Entry time")
    print("-" * 120)
    for i, t in enumerate(sim, 1):
        entry = str(t["entry_time"])[:16].replace("T", " ")
        print(f"{i:>2} {t['ticker']:<6} {t['direction']:<5} {t['entry_price']:>8.2f} {t['exit_price']:>8.2f} "
              f"{t['stop_price']:>8.2f} {t['target_price']:>8.2f} {t['pnl']:>+10.2f} {t['exit_reason']:<12} "
              f"{t['candles_held']:>4}  {entry}")

    # Match trades — time-aware sequential matching
    print(f"\n{'=' * 120}")
    print("COMPARISON (time-aware matching: same day + entry within tolerance)")
    print("=" * 120)

    PRICE_TOL = 1.0        # entry price tolerance
    TIME_TOL_HOURS = 6     # max hours between entries for a match

    def _parse_ts(t):
        """Parse entry_time to comparable datetime."""
        from datetime import datetime as dt
        s = str(t).replace("T", " ")[:19]
        try:
            return dt.fromisoformat(s)
        except Exception:
            return None

    matched_pairs = []       # (real_trade, sim_trade, kind)
    used_real = set()
    used_sim = set()

    # Pass 1: exact matches (same entry price + within TIME_TOL)
    for ri, r in enumerate(real):
        r_ts = _parse_ts(r["entry_time"])
        for si, s in enumerate(sim):
            if si in used_sim:
                continue
            s_ts = _parse_ts(s["entry_time"])
            time_ok = (r_ts and s_ts and abs((r_ts - s_ts).total_seconds()) < TIME_TOL_HOURS * 3600)
            if (r["ticker"] == s["ticker"]
                    and r["direction"] == s["direction"]
                    and abs(r["entry_price"] - s["entry_price"]) < 0.05
                    and time_ok):
                matched_pairs.append((r, s, "exact"))
                used_real.add(ri)
                used_sim.add(si)
                break

    # Pass 2: near matches (entry price within tolerance + within TIME_TOL)
    for ri, r in enumerate(real):
        if ri in used_real:
            continue
        r_ts = _parse_ts(r["entry_time"])
        best_si, best_dist = None, 999.0
        for si, s in enumerate(sim):
            if si in used_sim:
                continue
            s_ts = _parse_ts(s["entry_time"])
            time_ok = (r_ts and s_ts and abs((r_ts - s_ts).total_seconds()) < TIME_TOL_HOURS * 3600)
            if (r["ticker"] == s["ticker"]
                    and r["direction"] == s["direction"]
                    and abs(r["entry_price"] - s["entry_price"]) < PRICE_TOL
                    and time_ok):
                dist = abs(r["entry_price"] - s["entry_price"])
                if dist < best_dist:
                    best_si, best_dist = si, dist
        if best_si is not None:
            matched_pairs.append((r, sim[best_si], "near"))
            used_real.add(ri)
            used_sim.add(best_si)

    exact_count = sum(1 for _, _, kind in matched_pairs if kind == "exact")
    near_count = sum(1 for _, _, kind in matched_pairs if kind == "near")

    if matched_pairs:
        print(f"\nMatched ({len(matched_pairs)}, {exact_count} exact + {near_count} near):")
        for r, s, kind in matched_pairs:
            price_match = "OK" if abs(r["exit_price"] - s["exit_price"]) < 0.5 else "DIFF"
            reason_match = "OK" if r["exit_reason"] == s["exit_reason"] else "DIFF"
            tag = "" if kind == "exact" else " [NEAR]"
            entry_diff = f" (dp={abs(r['entry_price'] - s['entry_price']):.2f})" if kind == "near" else ""
            r_ts = str(r["entry_time"])[:16].replace("T", " ")
            s_ts = str(s["entry_time"])[:16].replace("T", " ")
            time_diff = ""
            rt, st = _parse_ts(r["entry_time"]), _parse_ts(s["entry_time"])
            if rt and st:
                mins = int(abs((rt - st).total_seconds()) / 60)
                if mins > 0:
                    time_diff = f" dt={mins}m"
            print(f"  {r['ticker']:<6} {r['direction']:<5} @ {r['entry_price']:>8.2f}{entry_diff}  "
                  f"exit: {r['exit_price']:>8.2f} vs {s['exit_price']:>8.2f} [{price_match}]  "
                  f"reason: {r['exit_reason']:<12} vs {s['exit_reason']:<12} [{reason_match}]{tag}{time_diff}")

    real_only = [r for ri, r in enumerate(real) if ri not in used_real]
    sim_only = [s for si, s in enumerate(sim) if si not in used_sim]

    if real_only:
        print(f"\nReal only ({len(real_only)}) — missing in simulation:")
        for t in real_only:
            entry = str(t["entry_time"])[:16].replace("T", " ")
            print(f"  {t['ticker']:<6} {t['direction']:<5} @ {t['entry_price']:>8.2f}  {entry}  reason={t['exit_reason']}")

    if sim_only:
        print(f"\nSim only ({len(sim_only)}) — not in real trades:")
        for t in sim_only:
            entry = str(t["entry_time"])[:16].replace("T", " ")
            print(f"  {t['ticker']:<6} {t['direction']:<5} @ {t['entry_price']:>8.2f}  {entry}  reason={t['exit_reason']}")

    real_pnl = sum(t["pnl"] for t in real)
    sim_pnl = sum(t["pnl"] for t in sim)
    total_real = len(real)
    print(f"\nReal P&L: {real_pnl:+.2f} ({total_real} trades)")
    print(f"Sim  P&L: {sim_pnl:+.2f} ({len(sim)} trades)")
    print(f"Match rate: {exact_count}/{total_real} exact, {near_count} near, "
          f"{len(real_only)} missing, {len(sim_only)} extra")
    print(f"Match rate: {exact_count}/{len(real)} exact + {near_count} near = {exact_count + near_count}/{len(real)}")


def show_setup_log():
    """Show setup_log entries if table exists (live bot diagnostics)."""
    conn = sqlite3.connect("data/traderbot.db")
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"""
            SELECT ts, ticker, strategy, direction, entry_price, stop_price,
                   target_price, action, market_price, open_positions, candle_time_30m
            FROM setup_log
            WHERE ts >= datetime('now', '-{SIM_DAYS} days')
            ORDER BY ts
        """).fetchall()
    except sqlite3.OperationalError:
        conn.close()
        return  # table doesn't exist yet
    conn.close()

    if not rows:
        return

    print(f"\n{'=' * 120}")
    print(f"SETUP LOG (live bot, last {SIM_DAYS} days): {len(rows)} entries")
    print("=" * 120)
    print(f"{'Time':<20} {'Ticker':<6} {'Dir':<5} {'Entry':>8} {'SL':>8} {'TP':>8} {'Action':<8} {'Mkt':>8} {'Open':>4} {'30m candle'}")
    print("-" * 120)
    for r in rows:
        ts = r["ts"][:19].replace("T", " ")
        mp = f"{r['market_price']:>8.2f}" if r["market_price"] else "     N/A"
        ct = str(r["candle_time_30m"])[:16] if r["candle_time_30m"] else ""
        print(f"{ts:<20} {r['ticker']:<6} {r['direction']:<5} {r['entry_price']:>8.2f} "
              f"{r['stop_price']:>8.2f} {r['target_price']:>8.2f} {r['action']:<8} "
              f"{mp} {r['open_positions']:>4}  {ct}")


if __name__ == "__main__":
    real = load_real_trades()
    logger.info("Loaded %d real trades", len(real))
    sim = run_simulation()
    logger.info("Got %d simulated trades", len(sim))
    compare(real, sim)
    show_setup_log()
