"""IBKR integration — direct REST API + MCP fallback for JCR Terminal.

Priority order:
  1. IBKR Client Portal REST API (localhost:5000 via Gateway/TWS, or OAuth)
  2. Demo data (standalone testing)

The MCP tools (mcp__Interactive_Brokers_IBKR__*) only work inside the
Claude Code harness and cannot be called from a standalone Python process.
"""

from __future__ import annotations

import asyncio
import logging
import os
import ssl
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
    bucket: str = "MED"
    weight_pct: float = 0.0
    prev_close: float = 0.0
    day_change_pct: float = 0.0
    week_change_pct: float = 0.0
    drift_pct: float = 0.0
    price_history: list[dict] = field(default_factory=list)
    daily_pnl: float = 0.0


@dataclass
class AccountSummary:
    nav: float = 0.0
    nav_currency: str = "EUR"
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
    mode: str = "demo"  # "live" or "demo"


# ---------------------------------------------------------------------------
# IBKR REST API client (Client Portal API)
# ---------------------------------------------------------------------------

class IBKRRestClient:
    """Calls the IBKR Client Portal REST API directly via aiohttp."""

    RATE_LIMIT_DELAY = 0.4
    MAX_RETRIES = 3
    BACKOFF_BASE = 2.0

    def __init__(self) -> None:
        self._base_url = os.getenv("IBKR_BASE_URL", "https://localhost:5000/v1/api")
        self._account_id = os.getenv("IBKR_ACCOUNT_ID", "")
        self._access_token = os.getenv("IBKR_ACCESS_TOKEN", "")
        self._snapshot = IBKRSnapshot()
        self._lock = asyncio.Lock()
        self._last_call: float = 0.0
        self._session: Any = None  # aiohttp.ClientSession

    def _headers(self) -> dict:
        h: dict = {"Content-Type": "application/json"}
        if self._access_token:
            h["Authorization"] = f"Bearer {self._access_token}"
        return h

    def _ssl_ctx(self) -> Any:
        # IBKR Gateway uses self-signed cert on localhost — skip verification
        if "localhost" in self._base_url or "127.0.0.1" in self._base_url:
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            return ctx
        return True  # use default SSL for remote endpoints

    async def _get_session(self) -> Any:
        import aiohttp
        if self._session is None or self._session.closed:
            connector = aiohttp.TCPConnector(ssl=self._ssl_ctx())
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers=self._headers(),
                timeout=aiohttp.ClientTimeout(total=10),
            )
        return self._session

    async def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_call
        if elapsed < self.RATE_LIMIT_DELAY:
            await asyncio.sleep(self.RATE_LIMIT_DELAY - elapsed)
        self._last_call = time.monotonic()

    async def _get(self, path: str, params: dict | None = None) -> Any:
        session = await self._get_session()
        url = f"{self._base_url}{path}"
        for attempt in range(self.MAX_RETRIES):
            try:
                await self._throttle()
                async with session.get(url, params=params) as resp:
                    if resp.status == 429:
                        wait = self.BACKOFF_BASE ** (attempt + 1)
                        log.warning("Rate limited on %s, waiting %.1fs", path, wait)
                        await asyncio.sleep(wait)
                        continue
                    resp.raise_for_status()
                    return await resp.json()
            except Exception as exc:
                log.error("GET %s attempt %d failed: %s", path, attempt + 1, exc)
                if attempt < self.MAX_RETRIES - 1:
                    await asyncio.sleep(self.BACKOFF_BASE ** attempt)
                else:
                    raise
        raise RuntimeError(f"All retries exhausted for {path}")

    async def _get_account_id(self) -> str:
        if self._account_id:
            return self._account_id
        data = await self._get("/portfolio/accounts")
        if isinstance(data, list) and data:
            self._account_id = data[0].get("id", "")
        return self._account_id

    async def fetch_account_summary(self) -> AccountSummary:
        account_id = await self._get_account_id()
        data = await self._get(f"/portfolio/{account_id}/summary")
        return self._parse_summary(data)

    async def fetch_positions(self) -> list[Position]:
        account_id = await self._get_account_id()
        all_positions: list[Position] = []
        page = 0
        while True:
            data = await self._get(f"/portfolio/{account_id}/positions/{page}")
            if not data:
                break
            batch = self._parse_positions(data)
            all_positions.extend(batch)
            if len(data) < 100:
                break  # last page
            page += 1
        return all_positions

    async def fetch_price_history(self, conid: str, period: str = "2w") -> list[dict]:
        try:
            data = await self._get("/iserver/marketdata/history", params={
                "conid": conid,
                "period": period,
                "bar": "1d",
            })
            return data.get("data", []) if isinstance(data, dict) else []
        except Exception as exc:
            log.warning("price_history conid=%s: %s", conid, exc)
            return []

    async def fetch_price_snapshots(self, conids: list[str]) -> dict[str, dict]:
        results: dict[str, dict] = {}
        # IBKR snapshot: fields 31=last, 84=bid, 86=ask, 7295=close
        for i in range(0, len(conids), 10):
            batch = conids[i:i+10]
            try:
                data = await self._get("/iserver/marketdata/snapshot", params={
                    "conids": ",".join(batch),
                    "fields": "31,84,86,7295,82",
                })
                if isinstance(data, list):
                    for item in data:
                        cid = str(item.get("conid", ""))
                        if cid:
                            results[cid] = item
            except Exception as exc:
                log.warning("price_snapshot batch failed: %s", exc)
            await asyncio.sleep(0.3)
        return results

    async def fetch_balances(self) -> dict:
        account_id = await self._get_account_id()
        try:
            return await self._get(f"/portfolio/{account_id}/ledger")
        except Exception as exc:
            log.warning("fetch_balances: %s", exc)
            return {}

    async def refresh(self) -> IBKRSnapshot:
        async with self._lock:
            try:
                summary = await self.fetch_account_summary()
                positions = await self.fetch_positions()
                balances = await self.fetch_balances()

                # Price history for week change (top 15 by value)
                top15 = sorted(positions, key=lambda p: -abs(p.market_value))[:15]
                for pos in top15:
                    bars = await self.fetch_price_history(pos.conid, "2w")
                    pos.price_history = bars
                    if len(bars) >= 5:
                        week_ago = float(bars[-min(7, len(bars))].get("c", pos.market_price) or pos.market_price)
                        if week_ago:
                            pos.week_change_pct = (pos.market_price - week_ago) / week_ago * 100

                # Day change from daily_pnl / (market_value - daily_pnl)
                for pos in positions:
                    if pos.daily_pnl and pos.market_value:
                        prev_val = pos.market_value - pos.daily_pnl
                        if prev_val > 0:
                            pos.day_change_pct = pos.daily_pnl / prev_val * 100

                free_cash = self._extract_free_cash(balances, summary)

                self._snapshot = IBKRSnapshot(
                    positions=positions,
                    summary=summary,
                    free_cash=free_cash,
                    last_updated=datetime.now(timezone.utc),
                    is_stale=False,
                    mode="live",
                )
                log.info("Live IBKR refresh: %d positions, NAV=%.2f %s",
                         len(positions), summary.nav, summary.nav_currency)
            except Exception as exc:
                log.error("IBKRRestClient.refresh failed: %s", exc)
                self._snapshot.is_stale = True
                self._snapshot.error = str(exc)
        return self._snapshot

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # -----------------------------------------------------------------------
    # Parsers
    # -----------------------------------------------------------------------

    def _parse_summary(self, data: Any) -> AccountSummary:
        if not data:
            return AccountSummary()

        def _v(key: str, alt: str = "", default: float = 0.0) -> float:
            for k in (key, alt, key.upper(), alt.upper()):
                val = data.get(k)
                if val is not None:
                    if isinstance(val, dict):
                        val = val.get("amount", val.get("value", default))
                    try:
                        return float(val or default)
                    except (TypeError, ValueError):
                        pass
            return default

        return AccountSummary(
            nav=_v("netliquidation", "net_liquidation"),
            nav_currency=data.get("currency", "EUR"),
            cash=_v("totalcashvalue", "total_cash_value"),
            buying_power=_v("buyingpower", "buying_power"),
            gross_position_value=_v("grosspositionvalue", "gross_position_value"),
            unrealized_pnl=_v("unrealizedpnl", "unrealized_pnl"),
            realized_pnl=_v("realizedpnl", "realized_pnl"),
            maintenance_margin=_v("maintenancemarginreq", "maintenance_margin"),
            timestamp=datetime.now(timezone.utc),
        )

    def _parse_positions(self, data: Any) -> list[Position]:
        positions = []
        items = data if isinstance(data, list) else data.get("positions", [])
        for item in items:
            try:
                # Support both camelCase (CPAPI) and snake_case (MCP response)
                conid = str(
                    item.get("conid") or item.get("contract_id") or item.get("contractId") or ""
                )
                ticker = (
                    item.get("ticker") or item.get("symbol") or
                    item.get("contract_description") or item.get("description") or ""
                )
                # contract_description from MCP often has exchange suffix — strip it
                if "@" in ticker:
                    ticker = ticker.split("@")[0].strip()

                pos = Position(
                    conid=conid,
                    ticker=ticker,
                    description=item.get("contract_description") or item.get("description") or ticker,
                    quantity=float(item.get("position") or item.get("quantity") or 0),
                    market_price=float(item.get("mktPrice") or item.get("market_price") or 0),
                    market_value=float(item.get("mktValue") or item.get("market_value") or 0),
                    avg_cost=float(item.get("avgCost") or item.get("average_price") or 0),
                    unrealized_pnl=float(item.get("unrealizedPnl") or item.get("unrealized_pnl") or 0),
                    realized_pnl=float(item.get("realizedPnl") or item.get("realized_pnl") or 0),
                    daily_pnl=float(item.get("dailyPnL") or item.get("daily_pnl") or 0),
                    currency=item.get("currency", "USD"),
                    asset_class=item.get("assetClass") or item.get("asset_class") or "STK",
                    bucket=item.get("bucket", "MED"),
                )
                if conid and ticker:
                    positions.append(pos)
            except Exception as exc:
                log.warning("Skipping malformed position: %s — %s", item, exc)
        return positions

    def _extract_free_cash(self, balances: dict, summary: AccountSummary) -> float:
        if isinstance(balances, list):
            for entry in balances:
                if entry.get("currency") == "BASE":
                    val = entry.get("cash_balance") or entry.get("cashBalance")
                    if val is not None:
                        try:
                            return float(val)
                        except (TypeError, ValueError):
                            pass
        elif isinstance(balances, dict):
            for key in ("availablefunds", "AvailableFunds", "available_funds", "cashbalance"):
                val = balances.get(key)
                if val is not None:
                    if isinstance(val, dict):
                        return float(val.get("amount", 0) or 0)
                    try:
                        return float(val or 0)
                    except (TypeError, ValueError):
                        pass
        return summary.cash

    @property
    def snapshot(self) -> IBKRSnapshot:
        return self._snapshot

    def is_configured(self) -> bool:
        """True if REST API credentials/URL are configured."""
        return bool(
            self._access_token or
            "localhost" in self._base_url or
            "127.0.0.1" in self._base_url
        )


# ---------------------------------------------------------------------------
# MCP-backed client (used from Claude Code context via injected caller)
# ---------------------------------------------------------------------------

class IBKRMCPClient:
    """Uses injected async MCP tool caller (only works inside Claude Code)."""

    RATE_LIMIT_DELAY = 0.5
    MAX_RETRIES = 3
    BACKOFF_BASE = 2.0

    def __init__(self) -> None:
        self._snapshot = IBKRSnapshot()
        self._lock = asyncio.Lock()
        self._last_call: float = 0.0
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
                return await self._mcp_call(tool, **kwargs)
            except Exception as exc:
                if "rate limit" in str(exc).lower() or "429" in str(exc):
                    wait = self.BACKOFF_BASE ** (attempt + 1)
                    await asyncio.sleep(wait)
                else:
                    if attempt == self.MAX_RETRIES - 1:
                        raise
                    await asyncio.sleep(self.BACKOFF_BASE ** attempt)
        raise RuntimeError(f"All retries exhausted for {tool}")

    async def refresh(self) -> IBKRSnapshot:
        async with self._lock:
            try:
                summary_raw = await self._call("get_account_summary")
                positions_raw = await self._call("get_account_positions")
                balances_raw = await self._call("get_account_balances")

                rest_client = IBKRRestClient()
                summary = rest_client._parse_summary(summary_raw)
                positions = rest_client._parse_positions(
                    positions_raw if isinstance(positions_raw, list)
                    else positions_raw.get("positions", [])
                )

                for pos in positions:
                    if pos.daily_pnl and pos.market_value:
                        prev_val = pos.market_value - pos.daily_pnl
                        if prev_val > 0:
                            pos.day_change_pct = pos.daily_pnl / prev_val * 100

                free_cash = rest_client._extract_free_cash(
                    balances_raw.get("balances", balances_raw) if isinstance(balances_raw, dict) else balances_raw,
                    summary
                )

                self._snapshot = IBKRSnapshot(
                    positions=positions,
                    summary=summary,
                    free_cash=free_cash,
                    last_updated=datetime.now(timezone.utc),
                    is_stale=False,
                    mode="live",
                )
                log.info("MCP IBKR refresh: %d positions, NAV=%.2f %s",
                         len(positions), summary.nav, summary.nav_currency)
            except Exception as exc:
                log.error("IBKRMCPClient.refresh failed: %s", exc)
                self._snapshot.is_stale = True
                self._snapshot.error = str(exc)
        return self._snapshot

    @property
    def snapshot(self) -> IBKRSnapshot:
        return self._snapshot


# ---------------------------------------------------------------------------
# Unified client — auto-selects best available backend
# ---------------------------------------------------------------------------

class IBKRClient:
    """
    Auto-selects backend in this order:
      1. Injected MCP caller (Claude Code harness)
      2. REST API client (Gateway on localhost or OAuth)
      3. Demo data
    """

    def __init__(self) -> None:
        self._mcp: Optional[IBKRMCPClient] = None
        self._rest = IBKRRestClient()
        self._demo_fn: Any = None
        self._snapshot = IBKRSnapshot()

    def set_mcp_caller(self, fn: Any) -> None:
        self._mcp = IBKRMCPClient()
        self._mcp.set_mcp_caller(fn)

    def set_demo_fn(self, fn: Any) -> None:
        self._demo_fn = fn

    async def refresh(self) -> IBKRSnapshot:
        # 1. MCP (Claude Code)
        if self._mcp is not None:
            snap = await self._mcp.refresh()
            if not snap.is_stale:
                self._snapshot = snap
                return snap
            log.warning("MCP refresh stale, trying REST")

        # 2. REST API
        if self._rest.is_configured():
            snap = await self._rest.refresh()
            if not snap.is_stale:
                self._snapshot = snap
                return snap
            log.warning("REST refresh stale, falling back to demo")

        # 3. Demo
        if self._demo_fn:
            demo_client = IBKRMCPClient()
            demo_client.set_mcp_caller(self._demo_fn)
            snap = await demo_client.refresh()
            snap.mode = "demo"
            self._snapshot = snap
        return self._snapshot

    async def close(self) -> None:
        await self._rest.close()

    @property
    def snapshot(self) -> IBKRSnapshot:
        return self._snapshot


# Singleton
ibkr_client = IBKRClient()
