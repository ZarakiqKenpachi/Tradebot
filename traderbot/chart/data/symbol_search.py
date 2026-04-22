"""Symbol search with local cache and recent history."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from traderbot.chart.data.provider import CandleProvider, SymbolInfo

logger = logging.getLogger(__name__)

# Common MOEX tickers — instant offline results before TV search completes
_KNOWN_MOEX = {
    "SBER": ("MOEX", "Sberbank", "stock"),
    "GAZP": ("MOEX", "Gazprom", "stock"),
    "VTBR": ("MOEX", "VTB Bank", "stock"),
    "ROSN": ("MOEX", "Rosneft", "stock"),
    "LKOH": ("MOEX", "Lukoil", "stock"),
    "GMKN": ("MOEX", "Nornickel", "stock"),
    "NVTK": ("MOEX", "Novatek", "stock"),
    "TATN": ("MOEX", "Tatneft", "stock"),
    "MGNT": ("MOEX", "Magnit", "stock"),
    "YNDX": ("MOEX", "Yandex", "stock"),
    "PLZL": ("MOEX", "Polyus Gold", "stock"),
    "MTSS": ("MOEX", "MTS", "stock"),
    "AFLT": ("MOEX", "Aeroflot", "stock"),
    "NLMK": ("MOEX", "NLMK", "stock"),
    "CHMF": ("MOEX", "Severstal", "stock"),
    "ALRS": ("MOEX", "Alrosa", "stock"),
    "MOEX": ("MOEX", "Moscow Exchange", "stock"),
    "POLY": ("MOEX", "Polymetal", "stock"),
    "PHOR": ("MOEX", "PhosAgro", "stock"),
    "RUAL": ("MOEX", "Rusal", "stock"),
}


class SymbolSearchService:
    """Symbol search combining local knowledge base and provider search."""

    def __init__(self, provider: CandleProvider, history_path: Path | None = None):
        self.provider = provider
        self._history_path = history_path or Path("data/symbol_history.json")
        self._recent: list[dict] = self._load_history()

    def search(self, query: str, limit: int = 20) -> list[SymbolInfo]:
        """Search symbols. Returns local matches first, then TV results."""
        query_upper = query.strip().upper()
        if not query_upper:
            return []

        results: list[SymbolInfo] = []
        seen: set[str] = set()

        # 1. Local known symbols
        for sym, (exch, desc, stype) in _KNOWN_MOEX.items():
            if query_upper in sym:
                info = SymbolInfo(sym, exch, desc, stype)
                key = info.full_symbol
                if key not in seen:
                    results.append(info)
                    seen.add(key)

        # 2. Recent history
        for item in self._recent:
            if query_upper in item.get("symbol", "").upper():
                info = SymbolInfo(
                    item["symbol"], item["exchange"],
                    item.get("description", ""), item.get("type", ""),
                )
                key = info.full_symbol
                if key not in seen:
                    results.append(info)
                    seen.add(key)

        # 3. Live search from provider
        try:
            tv_results = self.provider.search_symbol(query, limit=limit)
            for info in tv_results:
                key = info.full_symbol
                if key not in seen:
                    results.append(info)
                    seen.add(key)
        except Exception:
            logger.warning("[SEARCH] Provider search failed, using local results only")

        return results[:limit]

    def add_to_history(self, info: SymbolInfo) -> None:
        """Remember a selected symbol for future quick access."""
        entry = {
            "symbol": info.symbol,
            "exchange": info.exchange,
            "description": info.description,
            "type": info.symbol_type,
        }
        # Remove duplicates, put new at front
        self._recent = [
            r for r in self._recent
            if not (r["symbol"] == info.symbol and r["exchange"] == info.exchange)
        ]
        self._recent.insert(0, entry)
        self._recent = self._recent[:50]  # keep last 50
        self._save_history()

    def get_recent(self, limit: int = 10) -> list[SymbolInfo]:
        """Get recently used symbols."""
        return [
            SymbolInfo(r["symbol"], r["exchange"], r.get("description", ""), r.get("type", ""))
            for r in self._recent[:limit]
        ]

    def _load_history(self) -> list[dict]:
        try:
            if self._history_path.exists():
                return json.loads(self._history_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("[SEARCH] Failed to load symbol history")
        return []

    def _save_history(self) -> None:
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            self._history_path.write_text(
                json.dumps(self._recent, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.warning("[SEARCH] Failed to save symbol history")
