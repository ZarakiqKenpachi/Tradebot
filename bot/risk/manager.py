from bot.strategies.base import Signal


class RiskManager:
    def __init__(self, max_position_size: int = 10, stop_loss_pct: float = 0.05):
        self.max_position_size = max_position_size
        self.stop_loss_pct = stop_loss_pct

    def approve(self, signal: Signal, current_position: int, current_price: float, avg_price: float) -> bool:
        if signal == Signal.BUY and current_position >= self.max_position_size:
            return False

        if signal == Signal.SELL and avg_price > 0:
            loss_pct = (avg_price - current_price) / avg_price
            if loss_pct >= self.stop_loss_pct:
                return True

        return signal != Signal.HOLD
