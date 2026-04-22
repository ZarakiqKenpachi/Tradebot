"""Convert TradeRecord data into chart marker format."""
from __future__ import annotations

import json
from datetime import datetime


def trades_to_markers(trades: list[dict]) -> str:
    """Convert trade records to JSON markers for the chart.

    Each trade produces two markers: entry and exit.

    Args:
        trades: List of trade dicts with keys:
            ticker, direction, entry_price, exit_price, stop_price, target_price,
            pnl, entry_time, exit_time, entry_reason, exit_reason, qty, candles_held

    Returns:
        JSON string of marker array.
    """
    markers = []

    for t in trades:
        entry_ts = _to_timestamp(t.get("entry_time"))
        exit_ts = _to_timestamp(t.get("exit_time"))

        if entry_ts:
            markers.append({
                "time": entry_ts,
                "type": "entry",
                "direction": t.get("direction", "BUY"),
                "price": t.get("entry_price", 0),
                "stop_price": t.get("stop_price", 0),
                "target_price": t.get("target_price", 0),
                "entry_reason": t.get("entry_reason", ""),
                "qty": t.get("qty", 0),
                "trade_id": t.get("id"),
                "ticker": t.get("ticker", ""),
            })

        if exit_ts:
            markers.append({
                "time": exit_ts,
                "type": "exit",
                "direction": t.get("direction", "BUY"),
                "price": t.get("exit_price", 0),
                "pnl": t.get("pnl", 0),
                "exit_reason": t.get("exit_reason", ""),
                "entry_reason": t.get("entry_reason", ""),
                "candles_held": t.get("candles_held", 0),
                "trade_id": t.get("id"),
                "ticker": t.get("ticker", ""),
            })

    markers.sort(key=lambda m: m["time"])
    return json.dumps(markers)


def trade_to_price_lines(trade: dict) -> str:
    """Generate price lines for a selected trade (entry, SL, TP).

    Returns JSON array of line objects.
    """
    lines = []
    direction = trade.get("direction", "BUY")
    is_buy = direction == "BUY"

    if trade.get("entry_price"):
        lines.append({
            "price": trade["entry_price"],
            "color": "#26a69a" if is_buy else "#ef5350",
            "title": f"Entry {trade['entry_price']:.2f}",
            "style": 0,  # solid
        })

    if trade.get("stop_price"):
        lines.append({
            "price": trade["stop_price"],
            "color": "#ff9800",
            "title": f"SL {trade['stop_price']:.2f}",
            "style": 2,  # dashed
        })

    if trade.get("target_price"):
        lines.append({
            "price": trade["target_price"],
            "color": "#2962ff",
            "title": f"TP {trade['target_price']:.2f}",
            "style": 2,  # dashed
        })

    if trade.get("exit_price"):
        lines.append({
            "price": trade["exit_price"],
            "color": "#787b86",
            "title": f"Exit {trade['exit_price']:.2f}",
            "style": 3,  # dotted
        })

    return json.dumps(lines)


def _to_timestamp(value) -> int | None:
    """Convert various time formats to Unix timestamp."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, datetime):
        return int(value.timestamp())
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return int(dt.timestamp())
        except ValueError:
            return None
    return None
