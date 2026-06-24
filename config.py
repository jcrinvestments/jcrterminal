"""JCR Investments Terminal — Configuration & Risk Framework."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

# ---------------------------------------------------------------------------
# Risk bucket definitions
# ---------------------------------------------------------------------------

@dataclass
class BucketSpec:
    name: str
    label: str
    target_pct: float       # midpoint target
    min_pct: float
    max_pct: float
    color: str              # Rich color name
    description: str
    beta_max: Optional[float] = None
    beta_min: Optional[float] = None
    pe_min: Optional[float] = None
    pe_max: Optional[float] = None
    icr_min: Optional[float] = None
    de_max: Optional[float] = None
    roe_min: Optional[float] = None
    roe_max: Optional[float] = None


BUCKETS: dict[str, BucketSpec] = {
    "HEDGE": BucketSpec(
        name="HEDGE",
        label="Hedge",
        target_pct=2.5,
        min_pct=0.0,
        max_pct=5.0,
        color="bright_yellow",
        description="Inflation / tail-risk hedge (GLD, SLV, BITI…)",
        beta_max=0.3,
    ),
    "LOW": BucketSpec(
        name="LOW",
        label="Low Risk / Stabilizer",
        target_pct=27.5,
        min_pct=25.0,
        max_pct=30.0,
        color="cyan",
        description="Low-beta, dividend payers, high quality",
        beta_max=0.8,
        pe_min=10.0,
        pe_max=20.0,
        icr_min=8.0,
    ),
    "MED": BucketSpec(
        name="MED",
        label="Medium Risk / Engine",
        target_pct=42.5,
        min_pct=40.0,
        max_pct=45.0,
        color="green",
        description="Core growth, moderate leverage",
        beta_min=0.8,
        beta_max=1.2,
        de_max=1.5,
        roe_min=10.0,
        roe_max=20.0,
    ),
    "HIGH": BucketSpec(
        name="HIGH",
        label="High Risk / Turbo",
        target_pct=22.5,
        min_pct=20.0,
        max_pct=25.0,
        color="red",
        description="High-beta, negative earnings allowed",
        beta_min=1.5,
    ),
    "CASH": BucketSpec(
        name="CASH",
        label="Cash / DCA Reserve",
        target_pct=5.0,
        min_pct=3.0,
        max_pct=8.0,
        color="white",
        description="Free capital for DCA rotations",
    ),
}

# Drift threshold: flag if actual weight deviates more than this from target
DRIFT_ALERT_PCT = 2.0

# Concentration alert: flag if single position > this %
CONCENTRATION_ALERT_PCT = 8.0

# DCA trigger: position has fallen >= this % in 2 weeks
DCA_DROP_THRESHOLD_PCT = 10.0

# Minimum order size to keep IBKR commissions < 0.5%
IBKR_MIN_ORDER_EUR = 200.0

# ---------------------------------------------------------------------------
# Refresh intervals (seconds)
# ---------------------------------------------------------------------------

REFRESH = {
    "positions_market_hours": 60,
    "positions_off_hours": 300,
    "prices_market_hours": 30,
    "macro": 900,
    "news": 300,
    "status_bar": 5,
}

# ---------------------------------------------------------------------------
# Market hours (Amsterdam = CET/CEST, NYSE = EST/EDT)
# ---------------------------------------------------------------------------

AMS_OPEN = (9, 0)    # 09:00 CET
AMS_CLOSE = (17, 30) # 17:30 CET
NYSE_OPEN = (15, 30) # 15:30 CET (= 09:30 EST)
NYSE_CLOSE = (22, 0) # 22:00 CET (= 16:00 EST)

# ---------------------------------------------------------------------------
# Environment / secrets (loaded from .env via python-dotenv)
# ---------------------------------------------------------------------------

# IBKR MCP endpoint — override via IBKR_MCP_URL in .env
IBKR_MCP_URL = os.getenv("IBKR_MCP_URL", "https://api.ibkr.com/v1/api/mcp")

# Cash reserve floor — do not deploy below this (EUR).
# Default 200 EUR; set higher (e.g. 5000) for larger portfolios via .env
CASH_RESERVE_EUR = float(os.getenv("CASH_RESERVE_EUR", "200"))

# DCA rotation groups (assign tickers in .env or override here)
DCA_GROUPS: dict[str, list[str]] = {
    "A": [],  # first to buy on dip
    "B": [],  # second priority
    "C": [],  # third priority
}

# ---------------------------------------------------------------------------
# News sources / keywords for filtering
# ---------------------------------------------------------------------------

NEWS_PRIORITY_KEYWORDS = [
    "Fed", "Federal Reserve", "FOMC", "ECB", "interest rate",
    "CPI", "inflation", "GDP", "earnings", "revenue", "guidance",
    "dividend", "buyback", "merger", "acquisition",
]

NEWS_TAGS = {
    "FED": ["Federal Reserve", "FOMC", "Fed Chair", "Powell", "Fed funds"],
    "ECB": ["ECB", "Lagarde", "eurozone", "deposit rate"],
    "EARNINGS": ["earnings", "EPS", "revenue", "guidance", "quarterly results"],
    "MACRO": ["CPI", "GDP", "unemployment", "NFP", "inflation", "PCE", "PMI"],
    "SECTOR": ["sector", "industry", "ETF"],
}

# ---------------------------------------------------------------------------
# Macro indicators to display
# ---------------------------------------------------------------------------

MACRO_INDICATORS = [
    "fed_funds_rate",
    "fed_next_meeting_expectation",
    "us_cpi_yoy",
    "us_cpi_mom",
    "ecb_deposit_rate",
    "ecb_next_meeting_expectation",
    "us_gdp_growth",
    "us_10y_yield",
    "de_10y_yield",
    "eur_usd",
    "usd_dkk",
    "vix",
    "gold_spot",
    "wti_crude",
    "us_unemployment",
]
