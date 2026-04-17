"""
Paper Trading — forward-тестирование через T-Bank Sandbox.

Бот работает в реальном времени по тому же торговому циклу, что и live,
но все ордера размещаются в sandbox-аккаунте (без реальных денег).
Используются реальные рыночные данные и реальное исполнение через API.

Запуск: py -3.12 -m traderbot.paper_trade
        py -3.12 -m traderbot.paper_trade --balance 200000
        py -3.12 -m traderbot.paper_trade --tickers SBER,GAZP
"""
import argparse
import logging
import os
import pathlib
import signal
import threading
import time
from datetime import datetime, time as dt_time, timezone
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo

from traderbot.broker.tbank import TBankBroker
from traderbot.clients.db import Database
from traderbot.config import AppConfig, load_config
from traderbot.data.feed import DataFeed
from traderbot.execution.manager import ExecutionManager
from traderbot.journal.multi_writer import ClientJournalView, MultiTradeJournal
from traderbot.journal.sqlite_writer import SqliteTradeJournal
from traderbot.journal.writer import TradeJournal
from traderbot.risk.manager import RiskManager
from traderbot.state.client_view import ClientStateView
from traderbot.state.sqlite_store import SqliteStateStore
from traderbot.strategies.registry import get_strategy

logger = logging.getLogger(__name__)

MSK = ZoneInfo("Europe/Moscow")
_MARKET_OPEN = dt_time(7, 0)
_MARKET_CLOSE = dt_time(23, 50)

# Праздничные дни MOEX 2025-2026
_MOEX_HOLIDAYS: frozenset[str] = frozenset({
    "2025-01-01", "2025-01-02", "2025-01-03", "2025-01-06", "2025-01-07",
    "2025-01-08",
    "2025-02-24",
    "2025-03-10",
    "2025-05-01", "2025-05-02", "2025-05-08", "2025-05-09",
    "2025-06-12", "2025-06-13",
    "2025-11-03", "2025-11-04",
    "2025-12-31",
    "2026-01-01", "2026-01-02", "2026-01-07", "2026-01-08", "2026-01-09",
    "2026-02-23",
    "2026-03-09",
    "2026-05-01", "2026-05-04", "2026-05-11",
    "2026-06-12",
    "2026-11-04",
    "2026-12-31",
})

# Интервалы
RECONCILE_INTERVAL_SEC = 300
HEARTBEAT_INTERVAL_SEC = 3600

# Виртуальный client_id для paper trading (фиксированный)
PAPER_CLIENT_ID = 1


def is_market_open(now_msk: datetime) -> bool:
    if now_msk.weekday() >= 5:
        return False
    if now_msk.strftime("%Y-%m-%d") in _MOEX_HOLIDAYS:
        return False
    t = now_msk.time().replace(tzinfo=None)
    return _MARKET_OPEN <= t < _MARKET_CLOSE


def setup_logging() -> None:
    os.makedirs("logs", exist_ok=True)
    handler = RotatingFileHandler(
        "logs/paper_trade.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)
    root.addHandler(logging.StreamHandler())


def _init_sandbox(broker: TBankBroker, initial_balance: float) -> str:
    """Создать sandbox-аккаунт и пополнить его."""
    account_id = broker.sandbox_create_account("PaperTrading")
    broker.sandbox_fund(account_id, initial_balance)
    logger.info("[PAPER] Sandbox account %s funded with %.2f RUB", account_id, initial_balance)
    return account_id


def _init_paper_db(db_path: str) -> tuple[Database, SqliteStateStore, MultiTradeJournal]:
    """Инициализировать отдельную SQLite БД для paper trading.

    Таблицы clients, positions, trades, consecutive_sl создаются через
    стандартный init_schema. Затем вставляется виртуальный клиент
    (id=1, role=admin) для совместимости с ExecutionManager.
    """
    os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
    db = Database(db_path)
    db.init_schema()

    # Убедиться, что виртуальный клиент существует
    with db.cursor() as cur:
        cur.execute("SELECT id FROM clients WHERE id = ?", (PAPER_CLIENT_ID,))
        if cur.fetchone() is None:
            with db.write() as wcur:
                wcur.execute(
                    """
                    INSERT INTO clients (id, tg_chat_id, role, status, account_name, created_at, updated_at)
                    VALUES (?, 0, 'admin', 'active', 'paper_trading', datetime('now'), datetime('now'))
                    """,
                    (PAPER_CLIENT_ID,),
                )

    sqlite_state = SqliteStateStore(db)
    sqlite_journal = SqliteTradeJournal(db)

    os.makedirs("journal", exist_ok=True)
    csv_journal = TradeJournal("journal/paper_trades.csv")
    multi_journal = MultiTradeJournal(sqlite_journal, csv_journal)

    return db, sqlite_state, multi_journal


def main() -> None:
    setup_logging()

    parser = argparse.ArgumentParser(description="TraderBot Paper Trading (Sandbox)")
    parser.add_argument("--config", default=None, help="Path to config file")
    parser.add_argument("--balance", type=float, default=None, help="Initial sandbox balance (RUB)")
    parser.add_argument("--tickers", default=None, help="Comma-separated ticker list (default: all)")
    args = parser.parse_args()

    # Загрузить конфиг
    config_path = args.config or str(pathlib.Path(__file__).parent / "config.yaml")
    config = load_config(config_path)

    if config.paper_trading is None:
        logger.error("[PAPER] paper_trading section not found in config or TBANK_SANDBOX_TOKEN not set")
        return

    pt = config.paper_trading
    initial_balance = args.balance or pt.initial_balance

    # Выбрать тикеры
    if args.tickers:
        selected = args.tickers.split(",")
        tickers = {k: v for k, v in config.tickers.items() if k in selected}
    else:
        tickers = config.tickers

    if not tickers:
        logger.error("[PAPER] No tickers selected")
        return

    # 1. Sandbox broker (для торговли)
    sandbox_broker = TBankBroker(
        token=pt.token,
        sandbox=True,
        app_name="TraderBot-Paper",
    )
    account_id = _init_sandbox(sandbox_broker, initial_balance)

    # 2. Market data broker (реальные свечи — через market_data токен, не sandbox)
    md_broker = TBankBroker(
        token=config.market_data.token_env,
        sandbox=False,
        app_name="TraderBot-Paper-MD",
    )
    feed = DataFeed(md_broker)

    # 3. Отдельная БД для paper trading
    db, sqlite_state, multi_journal = _init_paper_db(pt.database_path)

    # 4. Загрузить lot_size и price_step из API
    logger.info("[PAPER] Loading instrument info for %d tickers...", len(tickers))
    for ticker_name, ticker_conf in tickers.items():
        try:
            lot_size, price_step = md_broker.get_instrument_info(ticker_conf.figi)
            ticker_conf.lot_size = lot_size
            ticker_conf.price_step = price_step
            logger.info("[PAPER] %s: lot_size=%d price_step=%.6f", ticker_name, lot_size, price_step)
        except Exception:
            logger.exception("[PAPER] Failed to get instrument info for %s", ticker_name)
            return

    # 5. Стратегии
    strategies = {}
    for ticker_name, ticker_conf in tickers.items():
        strategies[ticker_name] = get_strategy(ticker_conf.strategy)

    known_tickers_by_figi = {tc.figi: name for name, tc in tickers.items()}

    # 6. ExecutionManager (один, sandbox)
    balance = sandbox_broker.get_portfolio_balance(account_id)
    risk = RiskManager(config.risk_pct, config.max_position_pct, balance)
    state_view = ClientStateView(sqlite_state, PAPER_CLIENT_ID)
    journal_view = ClientJournalView(multi_journal, PAPER_CLIENT_ID)

    em = ExecutionManager(
        broker=sandbox_broker,
        risk=risk,
        journal=journal_view,
        state=state_view,
        notifier=None,
        account_id=account_id,
        commission_pct=config.commission_pct,
        max_candles_timeout=config.max_candles_timeout,
        max_consecutive_sl=config.max_consecutive_sl,
        max_daily_sl=config.max_daily_sl,
        client_id=PAPER_CLIENT_ID,
        is_admin=True,
    )

    # Восстановить позиции из предыдущего запуска (если есть)
    em.recover()

    # Флаг остановки
    _stop = threading.Event()

    def _shutdown_handler(signum, frame):
        logger.info("[PAPER] Signal %s received, shutting down...", signum)
        _stop.set()

    signal.signal(signal.SIGTERM, _shutdown_handler)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _shutdown_handler)

    # Начальный отчёт
    logger.info("=" * 60)
    logger.info("[PAPER] Paper Trading started")
    logger.info("[PAPER] Sandbox account: %s", account_id)
    logger.info("[PAPER] Balance: %.2f RUB", balance)
    logger.info("[PAPER] Tickers: %s", ", ".join(tickers.keys()))
    logger.info("[PAPER] Database: %s", pt.database_path)
    logger.info("=" * 60)

    last_reconcile = 0.0
    last_heartbeat = 0.0
    _market_was_open: bool | None = None

    try:
        while not _stop.is_set():
            try:
                now_msk = datetime.now(timezone.utc).astimezone(MSK)
                market_open = is_market_open(now_msk)

                if _market_was_open is False and market_open:
                    balance = sandbox_broker.get_portfolio_balance(account_id)
                    logger.info("[PAPER] Market opened. Balance: %.2f RUB", balance)

                if _market_was_open is True and not market_open:
                    balance = sandbox_broker.get_portfolio_balance(account_id)
                    active = sum(1 for p in em.positions.values() if p.status == "active")
                    pending = sum(1 for p in em.positions.values() if p.status == "pending")
                    logger.info(
                        "[PAPER] Market closed. Balance: %.2f RUB | "
                        "Active: %d | Pending: %d",
                        balance, active, pending,
                    )
                    _print_session_stats(db)

                _market_was_open = market_open

                if not market_open:
                    _stop.wait(timeout=config.poll_interval_sec)
                    continue

                # Reconcile
                if time.time() - last_reconcile >= RECONCILE_INTERVAL_SEC:
                    try:
                        em.reconcile_with_broker(known_tickers_by_figi)
                    except Exception:
                        logger.exception("[PAPER] Reconcile failed")
                    last_reconcile = time.time()

                # Heartbeat
                if time.time() - last_heartbeat >= HEARTBEAT_INTERVAL_SEC:
                    balance = sandbox_broker.get_portfolio_balance(account_id)
                    active = sum(1 for p in em.positions.values() if p.status == "active")
                    pending = sum(1 for p in em.positions.values() if p.status == "pending")
                    logger.info(
                        "[PAPER] %s MSK | Balance: %.2f | "
                        "Active: %d | Pending: %d",
                        now_msk.strftime("%H:%M"), balance, active, pending,
                    )
                    last_heartbeat = time.time()

                # Торговый цикл
                for ticker_name, ticker_conf in tickers.items():
                    figi = ticker_conf.figi
                    strategy = strategies[ticker_name]
                    timeframes = list(dict.fromkeys(["30m"] + strategy.required_timeframes))

                    try:
                        candles = feed.get_candles(
                            figi=figi, timeframes=timeframes, days=15,
                        )
                    except Exception:
                        logger.exception("[PAPER] Feed error for %s", ticker_name)
                        continue

                    if not candles:
                        continue

                    min_tf = strategy.required_timeframes[0]
                    current_price = candles[min_tf].iloc[-1]["close"]
                    last_candle_time = candles["30m"].index[-1].to_pydatetime()

                    shared_setup = strategy.find_setup(candles)

                    try:
                        if em.has_position(figi):
                            em.update(figi, current_price, last_candle_time)
                        elif shared_setup is not None:
                            if em.is_ticker_blocked(ticker_name):
                                logger.info("[PAPER] %s blocked (consecutive SL limit)", ticker_name)
                            else:
                                em.open_position(ticker_name, figi, shared_setup)
                    except Exception:
                        logger.exception("[PAPER] Tick error on %s", ticker_name)

                _stop.wait(timeout=config.poll_interval_sec)

            except KeyboardInterrupt:
                logger.info("[PAPER] Stopped by user (Ctrl+C)")
                break
            except Exception as e:
                logger.exception("[PAPER] Main loop error: %s", e)
                _stop.wait(timeout=config.poll_interval_sec)

    finally:
        # Итоговый отчёт
        balance = sandbox_broker.get_portfolio_balance(account_id)
        active = sum(1 for p in em.positions.values() if p.status == "active")
        logger.info("=" * 60)
        logger.info("[PAPER] Paper Trading stopped")
        logger.info("[PAPER] Final balance: %.2f RUB (started: %.2f)", balance, initial_balance)
        logger.info("[PAPER] P&L: %+.2f RUB (%+.2f%%)",
                    balance - initial_balance,
                    (balance - initial_balance) / initial_balance * 100)
        logger.info("[PAPER] Open positions: %d (SL/TP remain in sandbox)", active)
        _print_session_stats(db)
        logger.info("=" * 60)
        db.close()


def _print_session_stats(db: Database) -> None:
    """Вывести статистику сделок из paper trading БД."""
    try:
        with db.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) as cnt, "
                "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                "SUM(pnl) as total_pnl, "
                "SUM(commission) as total_comm "
                "FROM trades WHERE client_id = ?",
                (PAPER_CLIENT_ID,),
            )
            row = cur.fetchone()

        if row and row["cnt"] > 0:
            total = row["cnt"]
            wins = row["wins"] or 0
            pnl = row["total_pnl"] or 0.0
            comm = row["total_comm"] or 0.0
            wr = wins / total * 100 if total > 0 else 0
            logger.info(
                "[PAPER] Stats: %d trades | %d wins (%0.f%% WR) | "
                "P&L: %+.2f | Commission: %.2f",
                total, wins, wr, pnl, comm,
            )

            # По тикерам
            with db.cursor() as cur:
                cur.execute(
                    "SELECT ticker, COUNT(*) as cnt, "
                    "SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins, "
                    "SUM(pnl) as pnl "
                    "FROM trades WHERE client_id = ? "
                    "GROUP BY ticker ORDER BY pnl DESC",
                    (PAPER_CLIENT_ID,),
                )
                for r in cur.fetchall():
                    tk_wr = (r["wins"] or 0) / r["cnt"] * 100
                    logger.info(
                        "[PAPER]   %s: %d trades | WR %.0f%% | P&L: %+.2f",
                        r["ticker"], r["cnt"], tk_wr, r["pnl"] or 0,
                    )
        else:
            logger.info("[PAPER] Stats: no trades yet")
    except Exception:
        logger.exception("[PAPER] Failed to print stats")


if __name__ == "__main__":
    main()
