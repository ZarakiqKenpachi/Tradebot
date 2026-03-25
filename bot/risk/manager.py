class RiskManager:
    """1% account risk per trade, 1:3.5 risk-reward."""

    def __init__(self, risk_pct: float = 0.01):
        self.risk_pct = risk_pct

    def position_size(self, balance: float, entry_price: float, stop_price: float) -> int:
        """Return number of lots to trade given account balance and price levels."""
        risk_amount = balance * self.risk_pct
        risk_per_unit = abs(entry_price - stop_price)
        if risk_per_unit == 0:
            return 0
        return max(1, int(risk_amount / risk_per_unit))
