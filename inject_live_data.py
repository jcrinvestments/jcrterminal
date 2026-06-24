"""
inject_live_data.py — schrijft echte IBKR data naar data/snapshot.json.

Dit script wordt NIET door de gebruiker uitgevoerd. Het wordt door de
Claude Code AI aangeroepen omdat alleen Claude Code toegang heeft tot de
IBKR MCP tools. main.py leest het resulterende JSON bestand.

Gebruik: python inject_live_data.py  (vanuit Claude Code terminal)
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Verwerk ruwe MCP response naar snapshot dict
# ---------------------------------------------------------------------------

def parse_summary(d: dict) -> dict:
    def v(key: str, alt: str = "") -> float:
        for k in (key, alt):
            val = d.get(k)
            if val is not None:
                if isinstance(val, dict):
                    val = val.get("amount", val.get("value", 0))
                try:
                    return float(val or 0)
                except (TypeError, ValueError):
                    pass
        return 0.0
    return {
        "nav": v("net_liquidation", "netliquidation"),
        "nav_currency": d.get("currency", "EUR"),
        "cash": v("total_cash_value", "totalcashvalue"),
        "buying_power": v("buying_power", "buyingpower"),
        "gross_position_value": v("gross_position_value", "grosspositionvalue"),
        "unrealized_pnl": v("unrealized_pnl", "unrealizedpnl"),
        "realized_pnl": v("realized_pnl", "realizedpnl"),
    }


def parse_positions(items: list) -> list[dict]:
    out = []
    for item in items:
        ticker = (
            item.get("ticker") or item.get("symbol") or
            item.get("contract_description") or ""
        )
        if "@" in ticker:
            ticker = ticker.split("@")[0].strip()

        # Day change from daily_pnl
        mkt_val = float(item.get("market_value") or item.get("mktValue") or 0)
        daily_pnl = float(item.get("daily_pnl") or item.get("dailyPnL") or 0)
        day_chg = 0.0
        if daily_pnl and mkt_val:
            prev = mkt_val - daily_pnl
            if prev > 0:
                day_chg = daily_pnl / prev * 100

        out.append({
            "conid": str(item.get("contract_id") or item.get("conid") or item.get("contractId") or ""),
            "ticker": ticker,
            "description": item.get("contract_description") or item.get("description") or ticker,
            "quantity": float(item.get("position") or item.get("quantity") or 0),
            "market_price": float(item.get("market_price") or item.get("mktPrice") or 0),
            "market_value": mkt_val,
            "avg_cost": float(item.get("average_price") or item.get("avgCost") or 0),
            "unrealized_pnl": float(item.get("unrealized_pnl") or item.get("unrealizedPnl") or 0),
            "realized_pnl": float(item.get("realized_pnl") or item.get("realizedPnl") or 0),
            "daily_pnl": daily_pnl,
            "day_change_pct": day_chg,
            "week_change_pct": 0.0,  # filled by main.py via price history
            "currency": item.get("currency", "USD"),
            "asset_class": item.get("asset_class") or item.get("assetClass") or "STK",
            "bucket": item.get("bucket", "MED"),
        })
    return out


def parse_free_cash(balances: dict | list, cash_fallback: float) -> float:
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
        b = balances.get("balances", balances)
        if isinstance(b, list):
            return parse_free_cash(b, cash_fallback)
        for key in ("available_funds", "availablefunds", "cash_balance"):
            val = b.get(key)
            if val is not None:
                try:
                    return float(val or 0)
                except (TypeError, ValueError):
                    pass
    return cash_fallback


def write_snapshot(summary_raw: dict, positions_raw: list, balances_raw,
                   eur_usd: float = 1.08) -> str:
    summary = parse_summary(summary_raw)
    positions = parse_positions(positions_raw)
    free_cash = parse_free_cash(balances_raw, summary["cash"])

    snapshot = {
        "fetched_at": time.time(),
        "fetched_iso": datetime.now(timezone.utc).isoformat(),
        "eur_usd": eur_usd,
        "summary": summary,
        "positions": positions,
        "free_cash": free_cash,
    }

    os.makedirs("data", exist_ok=True)
    path = os.path.join(os.path.dirname(__file__), "data", "snapshot.json")
    with open(path, "w") as f:
        json.dump(snapshot, f, indent=2)
    return path


if __name__ == "__main__":
    # This block is called by Claude Code to write live data.
    # The actual MCP calls happen in the calling context (main.py's Claude Code env).
    print("inject_live_data.py — dit script wordt aangeroepen vanuit Claude Code.")
    print("Gebruik: from inject_live_data import write_snapshot")
