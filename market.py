"""Live market data via web search — prices, FX, VIX, commodities."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("jcr.market")

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class FXRate:
    pair: str
    rate: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class MarketData:
    eur_usd: float = 0.0
    usd_dkk: float = 0.0
    vix: float = 0.0
    vix_prev: float = 0.0
    gold_spot: float = 0.0
    wti_crude: float = 0.0
    us_10y_yield: float = 0.0
    us_10y_yield_prev: float = 0.0
    de_10y_yield: float = 0.0
    de_10y_yield_prev: float = 0.0
    timestamp: Optional[datetime] = None
    is_stale: bool = False

    # Derived
    @property
    def vix_trend(self) -> str:
        if self.vix > self.vix_prev + 0.5:
            return "▲"
        if self.vix < self.vix_prev - 0.5:
            return "▼"
        return "─"

    @property
    def us_10y_change(self) -> float:
        return self.us_10y_yield - self.us_10y_yield_prev

    @property
    def de_10y_change(self) -> float:
        return self.de_10y_yield - self.de_10y_yield_prev


# ---------------------------------------------------------------------------
# Market data fetcher (uses web search MCP via injected caller)
# ---------------------------------------------------------------------------

class MarketDataFetcher:
    def __init__(self) -> None:
        self._data = MarketData()
        self._web_search: Optional[object] = None  # injected
        self._lock = asyncio.Lock()

    def set_web_searcher(self, fn: object) -> None:
        self._web_search = fn

    async def _search(self, query: str) -> str:
        if self._web_search is None:
            return ""
        try:
            result = await self._web_search(query)
            if isinstance(result, list):
                return " ".join(str(r) for r in result)
            return str(result)
        except Exception as exc:
            log.warning("Web search failed for '%s': %s", query, exc)
            return ""

    def _parse_number(self, text: str, pattern: str) -> Optional[float]:
        """Extract first number matching a regex pattern from text."""
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            raw = m.group(1).replace(",", "")
            try:
                return float(raw)
            except ValueError:
                pass
        return None

    async def refresh(self) -> MarketData:
        async with self._lock:
            prev = self._data
            try:
                tasks = [
                    self._fetch_eur_usd(),
                    self._fetch_usd_dkk(),
                    self._fetch_vix(),
                    self._fetch_gold(),
                    self._fetch_wti(),
                    self._fetch_us_10y(),
                    self._fetch_de_10y(),
                ]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                (eur_usd, usd_dkk, vix, gold, wti, us10y, de10y) = results

                self._data = MarketData(
                    eur_usd=eur_usd if isinstance(eur_usd, float) else prev.eur_usd,
                    usd_dkk=usd_dkk if isinstance(usd_dkk, float) else prev.usd_dkk,
                    vix=vix if isinstance(vix, float) else prev.vix,
                    vix_prev=prev.vix or (vix if isinstance(vix, float) else 0.0),
                    gold_spot=gold if isinstance(gold, float) else prev.gold_spot,
                    wti_crude=wti if isinstance(wti, float) else prev.wti_crude,
                    us_10y_yield=us10y if isinstance(us10y, float) else prev.us_10y_yield,
                    us_10y_yield_prev=prev.us_10y_yield or (us10y if isinstance(us10y, float) else 0.0),
                    de_10y_yield=de10y if isinstance(de10y, float) else prev.de_10y_yield,
                    de_10y_yield_prev=prev.de_10y_yield or (de10y if isinstance(de10y, float) else 0.0),
                    timestamp=datetime.now(timezone.utc),
                    is_stale=False,
                )
            except Exception as exc:
                log.error("MarketDataFetcher.refresh failed: %s", exc)
                self._data.is_stale = True
        return self._data

    async def _fetch_eur_usd(self) -> Optional[float]:
        text = await self._search("EUR/USD exchange rate current price")
        return self._parse_number(text, r"(?:EUR/USD|EURUSD)[^\d]*?([\d]+\.[\d]{2,5})")

    async def _fetch_usd_dkk(self) -> Optional[float]:
        text = await self._search("USD/DKK exchange rate current price")
        return self._parse_number(text, r"(?:USD/DKK|USDDKK)[^\d]*?([\d]+\.[\d]{2,5})")

    async def _fetch_vix(self) -> Optional[float]:
        text = await self._search("VIX volatility index current level today")
        return self._parse_number(text, r"VIX[^\d]*?([\d]+\.[\d]{1,2})")

    async def _fetch_gold(self) -> Optional[float]:
        text = await self._search("gold spot price USD per ounce today")
        return self._parse_number(text, r"(?:gold|XAU)[^\$]*?\$?([\d,]+\.[\d]{0,2})")

    async def _fetch_wti(self) -> Optional[float]:
        text = await self._search("WTI crude oil price per barrel today")
        return self._parse_number(text, r"(?:WTI|crude oil)[^\$]*?\$?([\d]+\.[\d]{0,2})")

    async def _fetch_us_10y(self) -> Optional[float]:
        text = await self._search("US 10 year Treasury yield current rate today")
        return self._parse_number(text, r"10[- ]?year[^\d]*([\d]+\.[\d]{2,3})\s*%")

    async def _fetch_de_10y(self) -> Optional[float]:
        text = await self._search("Germany 10 year Bund yield current today")
        return self._parse_number(text, r"(?:Bund|10[- ]?year)[^\d]*([\d]+\.[\d]{2,3})\s*%")

    @property
    def data(self) -> MarketData:
        return self._data


# Singleton
market_fetcher = MarketDataFetcher()
