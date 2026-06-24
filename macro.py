"""Macro-economic indicators dashboard for JCR Terminal."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("jcr.macro")


@dataclass
class MacroIndicator:
    key: str
    label: str
    value: Optional[float] = None
    value_str: str = "—"
    unit: str = ""
    prev_value: Optional[float] = None
    trend: str = "─"          # ▲ ▼ ─
    sentiment: str = "neutral" # bullish / bearish / neutral
    timestamp: Optional[datetime] = None

    def format(self) -> str:
        if self.value_str and self.value_str != "—":
            return f"{self.value_str}{self.unit}"
        if self.value is not None:
            return f"{self.value:.2f}{self.unit}"
        return "—"


@dataclass
class MacroDashboard:
    indicators: dict[str, MacroIndicator] = field(default_factory=dict)
    last_updated: Optional[datetime] = None
    is_stale: bool = False
    economic_calendar: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Fetcher
# ---------------------------------------------------------------------------

class MacroFetcher:
    def __init__(self) -> None:
        self._dashboard = MacroDashboard()
        self._web_search: Optional[object] = None
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
            log.warning("Macro web search '%s' failed: %s", query, exc)
            return ""

    def _pct(self, text: str, *patterns: str) -> Optional[float]:
        for pat in patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                try:
                    return float(m.group(1).replace(",", ""))
                except ValueError:
                    pass
        return None

    def _sentiment_rate(self, value: float, neutral_low: float, neutral_high: float) -> str:
        """High rates = bearish for growth/equity."""
        if value > neutral_high:
            return "bearish"
        if value < neutral_low:
            return "bullish"
        return "neutral"

    def _build(self, key: str, label: str, value: Optional[float], unit: str,
               sentiment: str = "neutral", value_str: str = "") -> MacroIndicator:
        ind = self._dashboard.indicators.get(key)
        prev = ind.value if ind else None
        trend = "─"
        if value is not None and prev is not None:
            if value > prev + 0.01:
                trend = "▲"
            elif value < prev - 0.01:
                trend = "▼"
        return MacroIndicator(
            key=key, label=label, value=value,
            value_str=value_str or (f"{value:.2f}" if value is not None else "—"),
            unit=unit, prev_value=prev, trend=trend, sentiment=sentiment,
            timestamp=datetime.now(timezone.utc),
        )

    async def refresh(self) -> MacroDashboard:
        async with self._lock:
            tasks = {
                "fed_funds_rate": self._fetch_fed_funds(),
                "fed_next_meeting_expectation": self._fetch_fed_next(),
                "us_cpi_yoy": self._fetch_cpi_yoy(),
                "us_cpi_mom": self._fetch_cpi_mom(),
                "ecb_deposit_rate": self._fetch_ecb_rate(),
                "ecb_next_meeting_expectation": self._fetch_ecb_next(),
                "us_gdp_growth": self._fetch_gdp(),
                "us_10y_yield": self._fetch_us10y(),
                "de_10y_yield": self._fetch_de10y(),
                "eur_usd": self._fetch_eur_usd(),
                "usd_dkk": self._fetch_usd_dkk(),
                "vix": self._fetch_vix(),
                "gold_spot": self._fetch_gold(),
                "wti_crude": self._fetch_wti(),
                "us_unemployment": self._fetch_unemployment(),
            }
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            indicators: dict[str, MacroIndicator] = {}
            for key, res in zip(tasks.keys(), results):
                if isinstance(res, MacroIndicator):
                    indicators[key] = res
                else:
                    log.warning("Macro indicator %s failed: %s", key, res)
                    old = self._dashboard.indicators.get(key)
                    if old:
                        old.is_stale = True  # type: ignore[attr-defined]
                        indicators[key] = old

            cal = await self._fetch_economic_calendar()

            self._dashboard = MacroDashboard(
                indicators=indicators,
                last_updated=datetime.now(timezone.utc),
                is_stale=False,
                economic_calendar=cal,
            )
        return self._dashboard

    # -----------------------------------------------------------------------
    # Individual indicator fetchers
    # -----------------------------------------------------------------------

    async def _fetch_fed_funds(self) -> MacroIndicator:
        text = await self._search("Federal Reserve Fed funds rate current 2024 2025")
        val = self._pct(text,
            r"(?:fed funds|federal funds)[^\d]*([\d]+\.[\d]{1,2})\s*(?:%|percent|to)",
            r"rate of ([\d]+\.[\d]{1,2})\s*(?:%|percent)",
        )
        sentiment = self._sentiment_rate(val or 0, 2.0, 3.5)
        return self._build("fed_funds_rate", "Fed Funds Rate", val, "%", sentiment)

    async def _fetch_fed_next(self) -> MacroIndicator:
        text = await self._search("Fed next FOMC meeting rate expectation CME FedWatch probability 2025")
        # Try to extract expected rate or cut/hold/hike
        m = re.search(r"(cut|hike|hold|unchanged|lower|raise)[^\d]*([\d]+\.[\d])?", text, re.IGNORECASE)
        action = m.group(1).lower() if m else "hold"
        rate_m = re.search(r"([\d]+\.[\d]{2})\s*%", text)
        value_str = rate_m.group(0) if rate_m else "—"
        sentiment = "bullish" if "cut" in action or "lower" in action else ("bearish" if "hike" in action or "raise" in action else "neutral")
        return self._build("fed_next_meeting_expectation", "Fed Next Meeting", None, "", sentiment,
                           value_str=f"{action.capitalize()} {value_str}".strip())

    async def _fetch_cpi_yoy(self) -> MacroIndicator:
        text = await self._search("US CPI inflation year over year latest print 2025")
        val = self._pct(text,
            r"CPI[^\d]*([\d]+\.[\d]{1})\s*(?:%|percent)\s*(?:year[- ]over[- ]year|YoY|annual)",
            r"inflation[^\d]*([\d]+\.[\d]{1})\s*(?:%|percent)",
        )
        sentiment = "bearish" if (val or 0) > 3.0 else ("bullish" if (val or 0) < 2.0 else "neutral")
        return self._build("us_cpi_yoy", "US CPI YoY", val, "%", sentiment)

    async def _fetch_cpi_mom(self) -> MacroIndicator:
        text = await self._search("US CPI month over month latest print 2025")
        val = self._pct(text,
            r"CPI[^\d]*([\d]+\.[\d]{1})\s*(?:%|percent)\s*(?:month[- ]over[- ]month|MoM|monthly)",
            r"(?:monthly|MoM)[^\d]*([\d]+\.[\d]{1})\s*%",
        )
        sentiment = "bearish" if (val or 0) > 0.3 else "neutral"
        return self._build("us_cpi_mom", "US CPI MoM", val, "%", sentiment)

    async def _fetch_ecb_rate(self) -> MacroIndicator:
        text = await self._search("ECB deposit rate current 2025")
        val = self._pct(text,
            r"(?:deposit|ECB)[^\d]*([\d]+\.[\d]{1,2})\s*(?:%|percent)",
        )
        sentiment = self._sentiment_rate(val or 0, 1.5, 3.0)
        return self._build("ecb_deposit_rate", "ECB Deposit Rate", val, "%", sentiment)

    async def _fetch_ecb_next(self) -> MacroIndicator:
        text = await self._search("ECB next meeting rate decision expectation 2025")
        m = re.search(r"(cut|hike|hold|unchanged|lower|raise)", text, re.IGNORECASE)
        action = m.group(1).lower() if m else "hold"
        sentiment = "bullish" if "cut" in action or "lower" in action else ("bearish" if "hike" in action else "neutral")
        return self._build("ecb_next_meeting_expectation", "ECB Next Meeting", None, "",
                           sentiment, value_str=action.capitalize())

    async def _fetch_gdp(self) -> MacroIndicator:
        text = await self._search("US GDP growth rate latest quarter 2025 annualized")
        val = self._pct(text,
            r"GDP[^\d]*([-]?[\d]+\.[\d]{1})\s*(?:%|percent)",
            r"grew[^\d]*([-]?[\d]+\.[\d]{1})\s*(?:%|percent)",
        )
        sentiment = "bullish" if (val or 0) > 2.0 else ("bearish" if (val or 0) < 0 else "neutral")
        return self._build("us_gdp_growth", "US GDP QoQ", val, "%", sentiment)

    async def _fetch_us10y(self) -> MacroIndicator:
        text = await self._search("US 10-year Treasury yield today current")
        val = self._pct(text, r"10[- ]?year[^\d]*([\d]+\.[\d]{2,3})\s*%")
        sentiment = "bearish" if (val or 0) > 4.5 else ("bullish" if (val or 0) < 3.5 else "neutral")
        return self._build("us_10y_yield", "US 10Y Yield", val, "%", sentiment)

    async def _fetch_de10y(self) -> MacroIndicator:
        text = await self._search("Germany 10 year Bund yield today current")
        val = self._pct(text, r"(?:Bund|10[- ]?year)[^\d]*([\d]+\.[\d]{2,3})\s*%")
        return self._build("de_10y_yield", "DE 10Y Bund", val, "%", "neutral")

    async def _fetch_eur_usd(self) -> MacroIndicator:
        text = await self._search("EUR USD exchange rate current today")
        val = self._pct(text, r"EUR/?USD[^\d]*([\d]+\.[\d]{3,5})")
        return self._build("eur_usd", "EUR/USD", val, "", "neutral")

    async def _fetch_usd_dkk(self) -> MacroIndicator:
        text = await self._search("USD DKK exchange rate current today")
        val = self._pct(text, r"USD/?DKK[^\d]*([\d]+\.[\d]{2,4})")
        return self._build("usd_dkk", "USD/DKK", val, "", "neutral")

    async def _fetch_vix(self) -> MacroIndicator:
        text = await self._search("VIX fear index level today current")
        val = self._pct(text, r"VIX[^\d]*([\d]+\.[\d]{1,2})")
        sentiment = "bearish" if (val or 0) > 25 else ("bullish" if (val or 0) < 15 else "neutral")
        return self._build("vix", "VIX", val, "", sentiment)

    async def _fetch_gold(self) -> MacroIndicator:
        text = await self._search("gold spot price per ounce USD today")
        val = self._pct(text, r"(?:gold|XAU)[^\$]*\$?([\d,]+\.[\d]{0,2})")
        if val:
            val = float(str(val).replace(",", ""))
        return self._build("gold_spot", "Gold Spot", val, " USD/oz", "neutral")

    async def _fetch_wti(self) -> MacroIndicator:
        text = await self._search("WTI crude oil price per barrel today")
        val = self._pct(text, r"(?:WTI|crude)[^\$]*\$?([\d]+\.[\d]{0,2})")
        return self._build("wti_crude", "WTI Crude", val, " USD/bbl", "neutral")

    async def _fetch_unemployment(self) -> MacroIndicator:
        text = await self._search("US unemployment rate latest month 2025")
        val = self._pct(text, r"unemployment[^\d]*([\d]+\.[\d]{1})\s*(?:%|percent)")
        sentiment = "bearish" if (val or 0) > 5.0 else ("bullish" if (val or 0) < 4.0 else "neutral")
        return self._build("us_unemployment", "US Unemployment", val, "%", sentiment)

    async def _fetch_economic_calendar(self) -> list[dict]:
        text = await self._search(
            "economic calendar next 7 days CPI FOMC NFP Fed meeting 2025"
        )
        events: list[dict] = []
        # Basic extraction of dates and events from search results
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            date_m = re.search(r"(\w+ \d{1,2},?\s*202\d|\d{1,2}\s+\w+\s+202\d)", line)
            if date_m and any(kw in line for kw in ["CPI", "FOMC", "NFP", "Fed", "ECB", "GDP", "PCE", "PMI"]):
                events.append({"date": date_m.group(0), "event": line[:120]})
        return events[:10]

    @property
    def dashboard(self) -> MacroDashboard:
        return self._dashboard


# Singleton
macro_fetcher = MacroFetcher()
