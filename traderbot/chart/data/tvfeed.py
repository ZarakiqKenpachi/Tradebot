"""TradingView data provider — tvDatafeed (1m+) and raw WebSocket (sub-minute)."""
from __future__ import annotations

import json
import logging
import random
import re
import string
from datetime import datetime, timezone

import pandas as pd
from websocket import create_connection

logger = logging.getLogger(__name__)

# ── Timeframe mappings ───────────────────────────────────────

# tvDatafeed Interval enum names (1m and above)
_TF_MAP_TVDATA: dict[str, str] = {
    "1m": "in_1_minute",
    "3m": "in_3_minute",
    "5m": "in_5_minute",
    "15m": "in_15_minute",
    "30m": "in_30_minute",
    "45m": "in_45_minute",
    "1h": "in_1_hour",
    "2h": "in_2_hour",
    "4h": "in_4_hour",
    "1d": "in_daily",
    "1w": "in_weekly",
    "1M": "in_monthly",
}

# Raw WebSocket timeframe strings (any TF including seconds)
_TF_MAP_WS: dict[str, str] = {
    "1S": "1S", "5S": "5S", "10S": "10S", "15S": "15S", "30S": "30S", "45S": "45S",
    "1m": "1", "3m": "3", "5m": "5", "15m": "15", "30m": "30", "45m": "45",
    "1h": "60", "2h": "120", "4h": "240",
    "1d": "1D", "1w": "1W", "1M": "1M",
}

# Sub-minute timeframes that require Premium + raw WebSocket
_SECONDS_TFS = {"1S", "5S", "10S", "15S", "30S", "45S"}

ALL_TIMEFRAMES = list(_TF_MAP_WS.keys())


def _get_interval(timeframe: str):
    """Convert string timeframe to tvDatafeed Interval enum."""
    from tvDatafeed import Interval
    attr = _TF_MAP_TVDATA.get(timeframe)
    if attr is None:
        raise ValueError(f"Unsupported timeframe for tvDatafeed: {timeframe}")
    return getattr(Interval, attr)


# ── Raw WebSocket fetcher (for sub-minute candles) ───────────

class _TvWebSocket:
    """Low-level TradingView WebSocket client for any timeframe including seconds."""

    WS_URL = "wss://data.tradingview.com/socket.io/websocket"

    def __init__(self, auth_token: str = "unauthorized_user_token"):
        self._auth_token = auth_token
        self._ws = None

    def fetch_candles(
        self, symbol: str, timeframe: str, n_bars: int = 5000, timeout_sec: int = 30,
    ) -> list[dict]:
        """Fetch candles via raw WebSocket.

        Args:
            symbol: Full symbol like "MOEX:SBER" or "BINANCE:BTCUSDT"
            timeframe: WebSocket TF string like "15S", "1", "60", "1D"
            n_bars: Number of bars to request
            timeout_sec: Max wait time

        Returns:
            List of dicts with keys: time, open, high, low, close, volume
        """
        import time

        session = self._gen_id("qs_")
        chart = self._gen_id("cs_")
        series_key = "s1"
        symbol_key = "sym1"
        symbol_str = f'={{"symbol":"{symbol}","adjustment":"splits"}}'

        try:
            self._ws = create_connection(self.WS_URL, headers={
                "Origin": "https://data.tradingview.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/120.0.0.0",
            })

            self._send("set_auth_token", [self._auth_token])
            self._send("chart_create_session", [chart, ""])
            self._send("quote_create_session", [session])
            self._send("resolve_symbol", [chart, symbol_key, symbol_str])
            self._send("create_series", [chart, series_key, series_key, symbol_key, timeframe, n_bars])

            start = time.time()
            while time.time() - start < timeout_sec:
                try:
                    result = self._ws.recv()
                    parts = re.split(r"~m~\d+~m~", result)
                    for part in parts:
                        part = part.strip()
                        if not part:
                            continue
                        try:
                            data = json.loads(part)
                        except (json.JSONDecodeError, ValueError):
                            continue

                        msg_type = data.get("m", "")

                        if msg_type == "series_error":
                            error_detail = data.get("p", [None, None, None, "unknown"])[3]
                            logger.error("[WS] Series error: %s", error_detail)
                            return []

                        if msg_type == "timescale_update":
                            raw = data["p"][1].get(series_key, {}).get("s", [])
                            return self._parse_candles(raw)

                except Exception:
                    break

            logger.warning("[WS] Timeout fetching %s %s", symbol, timeframe)
            return []

        finally:
            if self._ws:
                try:
                    self._ws.close()
                except Exception:
                    pass
                self._ws = None

    def _send(self, func: str, params: list) -> None:
        msg = json.dumps({"m": func, "p": params}, separators=(",", ":"))
        self._ws.send(f"~m~{len(msg)}~m~{msg}")

    @staticmethod
    def _gen_id(prefix: str) -> str:
        return prefix + "".join(random.choice(string.ascii_lowercase) for _ in range(12))

    @staticmethod
    def _parse_candles(raw: list) -> list[dict]:
        """Parse raw WebSocket candle data into dicts."""
        candles = []
        for item in raw:
            v = item.get("v", [])
            if len(v) >= 6:
                candles.append({
                    "time": v[0],
                    "open": v[1],
                    "high": v[2],
                    "low": v[3],
                    "close": v[4],
                    "volume": v[5],
                })
        return candles


# ── Main Provider ────────────────────────────────────────────

def _sessionid_to_jwt(sessionid: str) -> str | None:
    """Convert a TradingView sessionid cookie to a WebSocket JWT auth token.

    tvDatafeed HTTP methods work with the sessionid cookie directly,
    but the raw WebSocket requires a JWT extracted from the chart page.
    """
    import re
    import urllib.request

    try:
        req = urllib.request.Request(
            "https://www.tradingview.com/chart/",
            headers={
                "Cookie": f"sessionid={sessionid}",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/125.0.0.0",
            },
        )
        resp = urllib.request.urlopen(req, timeout=15)
        html = resp.read().decode("utf-8", errors="ignore")
        m = re.search(r'"auth_token"\s*:\s*"([^"]+)"', html)
        if m:
            jwt = m.group(1)
            logger.info("[TVFEED] Extracted JWT from sessionid (%d chars)", len(jwt))
            return jwt
    except Exception:
        logger.exception("[TVFEED] Failed to extract JWT from sessionid")

    return None


class TvDatafeedProvider:
    """Candle provider using TradingView.

    - For 1m+ timeframes: uses tvDatafeed library (efficient, batch download)
    - For sub-minute (1S..45S): uses raw WebSocket (requires Premium auth_token)

    Usage:
        # Anonymous (1m+ only)
        provider = TvDatafeedProvider()

        # With Premium account (all timeframes including seconds)
        provider = TvDatafeedProvider(username="email", password="pass")
    """

    MAX_RETRIES = 3

    def __init__(
        self,
        username: str | None = None,
        password: str | None = None,
        auth_token: str | None = None,
    ):
        self._username = username
        self._password = password
        self._manual_token = auth_token  # sessionid from browser cookie
        self._tv = None
        self._auth_token = "unauthorized_user_token"  # JWT for WebSocket
        self._connected = False

    def _create_connection(self) -> None:
        """Create a new tvDatafeed connection."""
        from tvDatafeed import TvDatafeed

        self._tv = None
        self._auth_token = "unauthorized_user_token"

        # Method 1: Direct sessionid (most reliable for Google/Apple accounts)
        if self._manual_token:
            try:
                self._tv = TvDatafeed()
                self._tv.token = self._manual_token
                # Convert sessionid → JWT for WebSocket
                jwt = _sessionid_to_jwt(self._manual_token)
                self._auth_token = jwt or self._manual_token
                self._connected = True
                logger.info("[TVFEED] Connected with manual auth_token (Premium)")
                return
            except Exception:
                logger.warning("[TVFEED] Manual token connection failed")

        # Method 2: Username/password login
        if self._username and self._password:
            try:
                self._tv = TvDatafeed(username=self._username, password=self._password)
                self._auth_token = getattr(self._tv, "token", "unauthorized_user_token")
                if self._auth_token and self._auth_token != "unauthorized_user_token":
                    self._connected = True
                    logger.info("[TVFEED] Connected with Premium account (login)")
                    return
                else:
                    logger.warning(
                        "[TVFEED] Login failed — credentials rejected. "
                        "For Google/Apple accounts: set TV_AUTH_TOKEN in passes_tv.env instead. "
                        "Get it from browser: tradingview.com → DevTools → Application → "
                        "Cookies → sessionid"
                    )
            except Exception:
                logger.warning("[TVFEED] Login attempt failed, falling back to anonymous")

        # Method 3: Anonymous fallback (1m+ only, no sub-minute)
        try:
            self._tv = TvDatafeed()
            self._connected = True
            logger.info("[TVFEED] Connected (anonymous mode, 1m+ only)")
        except Exception:
            self._connected = False
            logger.exception("[TVFEED] Connection failed completely")
            raise

    def _ensure_connection(self) -> None:
        """Lazy init — connect on first use."""
        if self._tv is not None:
            return
        self._create_connection()

    def _reconnect(self) -> None:
        """Force reconnect (e.g., after 'Connection to remote host was lost')."""
        logger.info("[TVFEED] Reconnecting...")
        self._tv = None
        self._create_connection()

    @property
    def has_premium(self) -> bool:
        """True if authenticated with a TV account (enables sub-minute data)."""
        self._ensure_connection()
        return self._auth_token != "unauthorized_user_token"

    def get_candles(
        self,
        symbol: str,
        exchange: str,
        timeframe: str,
        n_bars: int = 5000,
    ) -> pd.DataFrame:
        """Fetch OHLCV candles from TradingView.

        For sub-minute timeframes (1S..45S), requires Premium account.

        Returns DataFrame with columns: open, high, low, close, volume
        Index: DatetimeIndex (UTC).
        """
        self._ensure_connection()

        if timeframe in _SECONDS_TFS:
            return self._fetch_via_websocket(symbol, exchange, timeframe, n_bars)
        else:
            return self._fetch_via_tvdata(symbol, exchange, timeframe, n_bars)

    def _fetch_via_tvdata(
        self, symbol: str, exchange: str, timeframe: str, n_bars: int,
    ) -> pd.DataFrame:
        """Fetch via tvDatafeed library (1m and above) with retry on connection loss."""
        interval = _get_interval(timeframe)

        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                df = self._tv.get_hist(
                    symbol=symbol, exchange=exchange,
                    interval=interval, n_bars=n_bars,
                )
            except Exception as exc:
                exc_msg = str(exc).lower()
                is_connection_error = any(s in exc_msg for s in (
                    "connection", "lost", "reset", "broken", "eof", "timeout",
                ))
                if is_connection_error and attempt < self.MAX_RETRIES:
                    logger.warning(
                        "[TVFEED] Connection lost fetching %s:%s %s (attempt %d/%d), reconnecting...",
                        exchange, symbol, timeframe, attempt, self.MAX_RETRIES,
                    )
                    import time
                    time.sleep(attempt * 2)  # backoff: 2s, 4s
                    self._reconnect()
                    continue
                logger.exception("[TVFEED] Failed to fetch %s:%s %s", exchange, symbol, timeframe)
                return pd.DataFrame()

            if df is None or df.empty:
                if attempt < self.MAX_RETRIES:
                    logger.warning(
                        "[TVFEED] No data for %s:%s %s (attempt %d/%d), retrying...",
                        exchange, symbol, timeframe, attempt, self.MAX_RETRIES,
                    )
                    self._reconnect()
                    continue
                logger.warning("[TVFEED] No data for %s:%s %s after %d attempts",
                               exchange, symbol, timeframe, self.MAX_RETRIES)
                return pd.DataFrame()

            return self._normalize_df(df, exchange, symbol, timeframe)

        return pd.DataFrame()

    def _fetch_via_websocket(
        self, symbol: str, exchange: str, timeframe: str, n_bars: int,
    ) -> pd.DataFrame:
        """Fetch via raw WebSocket (sub-minute timeframes)."""
        if self._auth_token == "unauthorized_user_token":
            logger.error("[TVFEED] Sub-minute timeframes require Premium. Use TvDatafeedProvider(username, password)")
            return pd.DataFrame()

        ws_tf = _TF_MAP_WS.get(timeframe, timeframe)
        full_symbol = f"{exchange}:{symbol}"

        ws = _TvWebSocket(self._auth_token)
        candles = ws.fetch_candles(full_symbol, ws_tf, n_bars)

        if not candles:
            logger.warning("[TVFEED] No WS data for %s %s", full_symbol, timeframe)
            return pd.DataFrame()

        df = pd.DataFrame(candles)
        df["time"] = pd.to_datetime(df["time"], unit="s", utc=True)
        df = df.set_index("time")
        df = df.sort_index()
        df = df.dropna(subset=["open", "close"])

        logger.info("[TVFEED] WS loaded %d bars for %s %s", len(df), full_symbol, timeframe)
        return df

    def search_symbol(self, query: str, limit: int = 20) -> list:
        """Search TradingView symbols."""
        from traderbot.chart.data.provider import SymbolInfo

        self._ensure_connection()
        try:
            results = self._tv.search_symbol(query)
        except Exception:
            logger.exception("[TVFEED] Symbol search failed for '%s'", query)
            return []

        if not results:
            return []

        symbols = []
        for item in results[:limit]:
            if isinstance(item, dict):
                symbols.append(SymbolInfo(
                    symbol=item.get("symbol", ""),
                    exchange=item.get("exchange", ""),
                    description=item.get("description", ""),
                    symbol_type=item.get("type", ""),
                ))
            elif isinstance(item, (list, tuple)) and len(item) >= 2:
                symbols.append(SymbolInfo(
                    symbol=str(item[0]),
                    exchange=str(item[1]),
                    description=str(item[2]) if len(item) > 2 else "",
                    symbol_type=str(item[3]) if len(item) > 3 else "",
                ))
        return symbols

    def set_auth_token(self, token: str) -> None:
        """Hot-swap auth token (e.g. after browser login). Reconnects."""
        self._manual_token = token
        self._tv = None
        self._auth_token = "unauthorized_user_token"
        self._connected = False
        self._create_connection()

    def is_connected(self) -> bool:
        return self._connected

    @staticmethod
    def _normalize_df(df: pd.DataFrame, exchange: str, symbol: str, timeframe: str) -> pd.DataFrame:
        """Normalize any DataFrame to standard OHLCV format with UTC DatetimeIndex."""
        cols = ["open", "high", "low", "close", "volume"]
        available = [c for c in cols if c in df.columns]
        df = df[available].copy()

        if not isinstance(df.index, pd.DatetimeIndex):
            df.index = pd.to_datetime(df.index)

        if df.index.tz is None:
            df.index = df.index.tz_localize("UTC")
        else:
            df.index = df.index.tz_convert("UTC")

        df = df.sort_index()
        df = df.dropna(subset=["open", "close"])

        logger.info("[TVFEED] Loaded %d bars for %s:%s %s", len(df), exchange, symbol, timeframe)
        return df
