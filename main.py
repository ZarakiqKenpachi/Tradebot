import os
import time

from dotenv import load_dotenv
from t_tech.invest import CandleInterval

from bot.broker.tinkoff import TinkoffBroker
from bot.data.storage import MarketDataStorage
from bot.execution.manager import ExecutionManager
from bot.logs.journal import TradeJournal
from bot.risk.manager import RiskManager
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

POLL_SECONDS = 60


def main():
    token = os.getenv("TINKOFF_TOKEN")
    if not token:
        raise ValueError("TINKOFF_TOKEN is not set. Add it to your .env file.")

    sandbox = os.getenv("SANDBOX", "false").lower() == "true"

    broker = TinkoffBroker(token=token, sandbox=sandbox)

    if sandbox:
        try:
            account_id = broker.get_account_id()
        except Exception:
            account_id = broker.sandbox_create_account()
            broker.sandbox_fund(account_id, amount=100_000.0, currency="rub")
    else:
        account_id = broker.get_account_id()

    storage = MarketDataStorage()
    strategy = ICTStrategy(risk_reward=2.0)
    risk = RiskManager(risk_pct=0.01)
    journal = TradeJournal()
    execution = ExecutionManager(broker=broker, risk=risk, journal=journal, account_id=account_id)

    print(f"TraderBot started | {len(TICKERS)} tickers | sandbox={sandbox} | account={account_id}")

    while True:
        try:
            for ticker, figi in TICKERS.items():
                try:
                    df_1m = broker.get_candles(figi, CandleInterval.CANDLE_INTERVAL_1_MIN, days=3)
                    if df_1m.empty:
                        print(f"[WARN] No candle data for {ticker}, skipping.")
                        continue

                    df_30m = storage.filter_moex_hours(storage.resample(df_1m, "30min").iloc[:-1])
                    df_1h  = storage.filter_moex_hours(storage.resample(df_1m, "1h").iloc[:-1])

                    execution.update(figi, df_30m)

                    if not execution.has_position(figi):
                        setup = strategy.find_setup(df_1h, df_30m)
                        if setup:
                            print(f"[SIGNAL] {ticker} {setup.direction.value} | entry={setup.entry_price}")
                            execution.open_position(figi, setup)
                        else:
                            print(f"[SCAN] {ticker}: no setup.")

                except Exception as e:
                    print(f"[ERROR] {ticker}: {e}")

        except Exception as e:
            print(f"[ERROR] {e}")

        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
