import math


class RiskManager:
    """Расчёт размера позиции и лимиты по риску."""

    def __init__(self, risk_pct: float = 0.10, max_position_pct: float = 0.40, deposit: float = 0.0):
        self.risk_pct = risk_pct
        self.max_position_pct = max_position_pct
        self.deposit = deposit  # Фиксированный депозит для расчёта лимита позиции

    def position_size(self, balance: float, entry_price: float, stop_price: float, lot_size: int = 1) -> int:
        """
        Рассчитать размер позиции в лотах.

        1. risk_amount = balance * risk_pct                          (от текущего баланса)
        2. risk_per_lot = abs(entry_price - stop_price) * lot_size   (риск на один лот)
        3. qty = floor(risk_amount / risk_per_lot)
        4. max_qty = floor(deposit * max_position_pct / (entry_price * lot_size))  (лимит по депозиту)
        5. return min(qty, max_qty), но не менее 1

        Возвращает 0 если позиция невозможна.
        """
        risk_per_lot = abs(entry_price - stop_price) * lot_size
        if risk_per_lot == 0:
            return 0

        risk_amount = balance * self.risk_pct
        qty = math.floor(risk_amount / risk_per_lot)

        max_qty = math.floor(self.deposit * self.max_position_pct / (entry_price * lot_size))

        result = min(qty, max_qty)
        return result if result >= 1 else 0
