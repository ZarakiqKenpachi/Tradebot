class TinkoffBroker:
    def __init__(self, token: str):
        self.token = token

    def get_portfolio(self):
        raise NotImplementedError

    def place_order(self, ticker: str, quantity: int, direction: str):
        raise NotImplementedError

    def get_candles(self, ticker: str, interval: str, days: int):
        raise NotImplementedError
