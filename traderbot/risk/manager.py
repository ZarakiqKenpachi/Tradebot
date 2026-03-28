import math


class RiskManager:
    """Расчёт размера позиции и лимиты по риску."""

    def __init__(self, risk_pct: float = 0.10, max_position_pct: float = 0.40):
        self.risk_pct = risk_pct
        self.max_position_pct = max_position_pct

    def position_size(self, balance: float, entry_price: float, stop_price: float) -> int:
        """
        Рассчитать размер позиции в лотах.

        1. risk_amount = balance * risk_pct
        2. risk_per_unit = abs(entry_price - stop_price)
        3. qty = floor(risk_amount / risk_per_unit)
        4. max_qty = floor(balance * max_position_pct / entry_price)
        5. return min(qty, max_qty), но не менее 1

        Возвращает 0 если позиция невозможна.
        """
        risk_per_unit = abs(entry_price - stop_price)
        if risk_per_unit == 0:
            return 0

        risk_amount = balance * self.risk_pct
        qty = math.floor(risk_amount / risk_per_unit)

        max_qty = math.floor(balance * self.max_position_pct / entry_price)

        result = min(qty, max_qty)
        return result if result >= 1 else 0
