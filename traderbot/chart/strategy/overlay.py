"""Calculate indicator overlays for the chart."""
from __future__ import annotations

import json

import pandas as pd


def calc_ema(series: pd.Series, period: int) -> pd.Series:
    """Calculate Exponential Moving Average."""
    return series.ewm(span=period, adjust=False).mean()


def calc_sma(series: pd.Series, period: int) -> pd.Series:
    """Calculate Simple Moving Average."""
    return series.rolling(window=period).mean()


def calc_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Calculate Average True Range."""
    h = df["high"]
    l = df["low"]
    pc = df["close"].shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.rolling(period).mean()


def calc_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculate Relative Strength Index."""
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = (-delta).clip(lower=0)
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def series_to_chart_json(series: pd.Series) -> str:
    """Convert a pandas Series to JSON array of {time, value} for Lightweight Charts."""
    data = []
    for ts, val in series.items():
        if pd.notna(val):
            data.append({
                "time": int(ts.timestamp()),
                "value": round(float(val), 6),
            })
    return json.dumps(data)


def df_to_chart_json(df: pd.DataFrame) -> str:
    """Convert OHLCV DataFrame to JSON array for chart.setCandles()."""
    data = []
    for ts, row in df.iterrows():
        data.append({
            "time": int(ts.timestamp()),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
            "volume": float(row.get("volume", 0)),
        })
    return json.dumps(data)
