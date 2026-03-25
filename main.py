from bot.broker.tinkoff import TinkoffBroker
from bot.data.storage import MarketDataStorage
from bot.risk.manager import RiskManager


def main():
    token = "YOUR_TINKOFF_TOKEN"

    broker = TinkoffBroker(token=token)
    storage = MarketDataStorage()
    risk = RiskManager()

    print("TraderBot started.")


if __name__ == "__main__":
    main()
