"""IBKR MCP integration layer for JCR Terminal."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger("jcr.ibkr")

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass
class Position:
    conid: str
    ticker: str
    description: str
    quantity: float
    market_price: float
    market_value: float
    avg_cost: float
    unrealized_pnl: float
    realized_pnl: float
    currency: str
    asset_class: str
    bucket: str = "MED"       # assigned from config
    weight_pct: float = 0.0
    prev_close: float = 0.0
    day_change_pct: float = 0.0
    week_change_pct: float = 0.0
    drift_pct: float = 0.0    # actual weight - bucket target
    price_history: list[dict] = field(default_factory=list)


@dataclass
class AccountSummary:
    nav: float = 0.0
    nav_currency: str = "USD"
    cash: float = 0.0
    buying_power: float = 0.0
    gross_position_value: float = 0.0
    unrealized_pnl: float = 0.0
    realized_pnl: float = 0.0
    maintenance_margin: float = 0.0
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass
class IBKRSnapshot:
    positions: list[Position] = field(default_factory=list)
    summary: AccountSummary = field(default_factory=AccountSummary)
    free_cash: float = 0.0
    last_updated: Optional[datetime] = None
    is_stale: bool = False
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# Rate-limit aware fetcher
# ---------------------------------------------------------------------------

class IBKRClient:
    """Thin async wrapper around the IBKR MCP server tools."""

    RATE_LIMIT_DELAY = 0.5   # seconds between calls
    MAX_RETRIES = 3
    BACKOFF_BASE = 2.0

    def __init__(self) -> None:
        self._last_call: float = 0.0
        self._snapshot = IBKRSnapshot()
        self._lock = asyncio.Lock()
        # Will be injected by main.py with the actual MCP call function
        self._mcp_call: Any = None

    def set_mcp_caller(self, fn: Any) -> None:
        self._mcp_call = fn

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.RATE_LIMIT_DELAY:
            await asyncio.sleep(self.RATE_LIMIT_DELAY - elapsed)
        self._last_call = time.monotonic()

    async def _call(self, tool: str, **kwargs: Any) -> Any:
        if self._mcp_call is None:
            raise RuntimeError("MCP caller not configured")
        for attempt in range(self.MAX_RETRIES):
            try:
                await self._throttle()
                result = await self._mcp_call(tool, **kwargs)
                return result
            except Exception as exc:
                if "rate limit" in str(exc).lower() or "429" in str(exc):
                    wait = self.BACKOFF_BASE ** (attempt + 1)
                    log.warning("Rate limited on %s, waiting %.1fs", tool, wait)
                    await asyncio.sleep(wait)
                else:
                    log.error("IBKR call %s attempt %d failed: %s", tool, attempt + 1, exc)
                    if attempt == self.MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(self.BACKOFF_BASE ** attempt)
        raise RuntimeError(f"All retries exhausted for {tool}")

    # -----------------------------------------------------------------------
    # Public fetch methods
    # -----------------------------------------------------------------------

    async def fetch_account_summary(self) -> AccountSummary:
        try:
            data = await self._call("get_account_summary")
            return self._parse_account_summary(data)
        except Exception as exc:
            log.error("fetch_account_summary failed: %s", exc)
            return self._snapshot.summary

    async def fetch_positions(self) -> list[Position]:
        try:
            data = await self._call("get_account_positions")
            positions = self._parse_positions(data)
            return positions
        except Exception as exc:
            log.error("fetch_positions failed: %s", exc)
            return self._snapshot.positions

    async def fetch_price_snapshots(self, conids: list[str]) -> dict[str, dict]:
        """Fetch live prices for a list of conids (batch up to 15)."""
        results: dict[str, dict] = {}
        # Process in batches of 15
        for i in range(0, len(conids), 15):
            batch = conids[i : i + 15]
            try:
                data = await self._call("get_price_snapshot", conids=",".join(batch))
                if isinstance(data, list):
                    for item in data:
                        cid = str(item.get("conid", ""))
                        if cid:
                            results[cid] = item
                elif isinstance(data, dict):
                    for cid, item in data.items():
                        results[str(cid)] = item
            except Exception as exc:
                log.error("fetch_price_snapshots batch %d failed: %s", i, exc)
            await asyncio.sleep(0.3)
        return results

    async def fetch_price_history(self, conid: str, period: str = "2w") -> list[dict]:
        """Return list of OHLCV bars for the requested period."""
        try:
            data = await self._call(
                "get_price_history",
                conid=conid,
                period=period,
                bar="1d",
            )
            bars = data if isinstance(data, list) else data.get("data", [])
            return bars
        except Exception as exc:
            log.error("fetch_price_history conid=%s failed: %s", conid, exc)
            return []

    async def fetch_balances(self) -> dict:
        try:
            return await self._call("get_account_balances")
        except Exception as exc:
            log.error("fetch_balances failed: %s", exc)
            return {}

    async def refresh(self) -> IBKRSnapshot:
        """Full portfolio refresh — called by the background task."""
        async with self._lock:
            try:
                summary = await self.fetch_account_summary()
                positions = await self.fetch_positions()
                balances = await self.fetch_balances()

                # Fetch price history for 7-day delta (TWO_WEEKS period)
                top15_conids = [p.conid for p in positions[:15]]
                snap_data = await self.fetch_price_snapshots(top15_conids)

                for pos in positions:
                    # Live price snapshot
                    snap = snap_data.get(pos.conid, {})
                    if snap:
                        pos.market_price = float(snap.get("last_price", pos.market_price) or pos.market_price)
                        pos.prev_close = float(snap.get("close", pos.market_price) or pos.market_price)
                        if pos.prev_close:
                            pos.day_change_pct = (pos.market_price - pos.prev_close) / pos.prev_close * 100

                # Compute week changes via price history
                for pos in positions[:15]:
                    bars = await self.fetch_price_history(pos.conid, period="2w")
                    pos.price_history = bars
                    if len(bars) >= 5:
                        # Use close from ~7 trading days ago
                        week_ago_close = float(bars[-min(7, len(bars))].get("c", pos.market_price) or pos.market_price)
                        if week_ago_close:
                            pos.week_change_pct = (pos.market_price - week_ago_close) / week_ago_close * 100

                # Free cash = total cash - reserve (handled in main/dca)
                free_cash = self._extract_free_cash(balances, summary)

                self._snapshot = IBKRSnapshot(
                    positions=positions,
                    summary=summary,
                    free_cash=free_cash,
                    last_updated=datetime.now(timezone.utc),
                    is_stale=False,
                )
            except Exception as exc:
                log.error("IBKRClient.refresh failed: %s", exc)
                self._snapshot.is_stale = True
                self._snapshot.error = str(exc)

        return self._snapshot

    # -----------------------------------------------------------------------
    # Parsers
    # -----------------------------------------------------------------------

    def _parse_account_summary(self, data: Any) -> AccountSummary:
        if not data:
            return AccountSummary()

        def _get(key: str, default: float = 0.0) -> float:
            val = data.get(key, default)
            if isinstance(val, dict):
                val = val.get("amount", default)
            try:
                return float(val or default)
            except (TypeError, ValueError):
                return default

        return AccountSummary(
            nav=_get("netliquidation") or _get("NetLiquidation") or _get("nav"),
            nav_currency=data.get("currency", "USD"),
            cash=_get("totalcashvalue") or _get("TotalCashValue"),
            buying_power=_get("buyingpower") or _get("BuyingPower"),
            gross_position_value=_get("grosspositionvalue") or _get("GrossPositionValue"),
            unrealized_pnl=_get("unrealizedpnl") or _get("UnrealizedPnL"),
            realized_pnl=_get("realizedpnl") or _get("RealizedPnL"),
            maintenance_margin=_get("maintenancemarginreq") or _get("MaintMarginReq"),
            timestamp=datetime.now(timezone.utc),
        )

    def _parse_positions(self, data: Any) -> list[Position]:
        positions = []
        if not data:
            return positions
        items = data if isinstance(data, list) else data.get("positions", [])
        for item in items:
            try:
                pos = Position(
                    conid=str(item.get("conid", "") or item.get("contractId", "")),
                    ticker=item.get("ticker", "") or item.get("symbol", ""),
                    description=item.get("description", "") or item.get("name", ""),
                    quantity=float(item.get("position", 0) or item.get("quantity", 0) or 0),
                    market_price=float(item.get("mktPrice", 0) or item.get("market_price", 0) or 0),
                    market_value=float(item.get("mktValue", 0) or item.get("market_value", 0) or 0),
                    avg_cost=float(item.get("avgCost", 0) or item.get("avg_cost", 0) or 0),
                    unrealized_pnl=float(item.get("unrealizedPnl", 0) or 0),
                    realized_pnl=float(item.get("realizedPnl", 0) or 0),
                    currency=item.get("currency", "USD"),
                    asset_class=item.get("assetClass", "") or item.get("asset_class", ""),
                    bucket=item.get("bucket", "MED"),
                )
                if pos.conid and pos.ticker:
                    positions.append(pos)
            except Exception as exc:
                log.warning("Skipping malformed position: %s — %s", item, exc)
        return positions

    def _extract_free_cash(self, balances: dict, summary: AccountSummary) -> float:
        if not balances:
            return summary.cash
        # Try to find available cash from balances response
        for key in ("availablefunds", "AvailableFunds", "available_funds"):
            val = balances.get(key)
            if val is not None:
                try:
                    if isinstance(val, dict):
                        return float(val.get("amount", 0) or 0)
                    return float(val or 0)
                except (TypeError, ValueError):
                    pass
        return summary.cash

    @property
    def snapshot(self) -> IBKRSnapshot:
        return self._snapshot


# Singleton instance
ibkr_client = IBKRClient()
