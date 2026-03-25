class RiskManager:
    """1% account risk per trade, max 40% of portfolio per position."""

    def __init__(self, risk_pct: float = 0.01, max_position_pct: float = 0.40):
        self.risk_pct = risk_pct
        self.max_position_pct = max_position_pct

    def position_size(self, balance: float, entry_price: float, stop_price: float) -> int:
        """Return number of lots to trade given account balance and price levels."""
        risk_amount = balance * self.risk_pct
        risk_per_unit = abs(entry_price - stop_price)
        if risk_per_unit == 0:
            return 0
        qty = int(risk_amount / risk_per_unit)
        max_qty = int(balance * self.max_position_pct / entry_price)
        return max(1, min(qty, max_qty))
