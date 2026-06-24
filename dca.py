"""DCA suggestion engine for JCR Terminal."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import (
    BUCKETS,
    CASH_RESERVE_EUR,
    DCA_DROP_THRESHOLD_PCT,
    DCA_GROUPS,
    IBKR_MIN_ORDER_EUR,
)
from ibkr import IBKRSnapshot, Position

log = logging.getLogger("jcr.dca")


@dataclass
class DCASuggestion:
    ticker: str
    conid: str
    bucket: str
    current_weight_pct: float
    target_weight_pct: float
    underweight_pct: float          # how far below target
    week_change_pct: float          # negative = dip
    priority_score: float           # higher = buy first
    suggested_order_eur: float
    min_order_eur: float
    dca_group: str
    reason: str

    @property
    def is_dip(self) -> bool:
        return self.week_change_pct <= -DCA_DROP_THRESHOLD_PCT

    @property
    def priority_label(self) -> str:
        if self.priority_score >= 20:
            return "[bold red]HIGH[/]"
        if self.priority_score >= 10:
            return "[yellow]MED[/]"
        return "[cyan]LOW[/]"


@dataclass
class WeekOverview:
    top_winners: list[tuple[str, float]] = field(default_factory=list)
    top_losers: list[tuple[str, float]] = field(default_factory=list)
    bucket_allocations: dict[str, float] = field(default_factory=dict)
    bucket_targets: dict[str, float] = field(default_factory=dict)
    cash_pct: float = 0.0
    cash_target_pct: float = 5.0
    nav: float = 0.0
    free_cash: float = 0.0
    action_items: list[str] = field(default_factory=list)


@dataclass
class RiskFlag:
    ticker: str
    flag_type: str
    description: str
    severity: str   # HIGH / MED / LOW
    color: str


def assign_bucket(pos: Position, bucket_map: dict[str, str]) -> str:
    """Return bucket from explicit map, fall back to existing assignment."""
    return bucket_map.get(pos.ticker, pos.bucket)


def compute_suggestions(
    snapshot: IBKRSnapshot,
    bucket_map: dict[str, str],
    eur_usd: float = 1.08,
) -> list[DCASuggestion]:
    if not snapshot.positions or snapshot.summary.nav <= 0:
        return []

    nav = snapshot.summary.nav
    # Convert NAV to EUR if needed
    nav_eur = nav / eur_usd if eur_usd > 0 else nav
    free_cash_eur = (snapshot.free_cash - CASH_RESERVE_EUR) / eur_usd

    if free_cash_eur <= IBKR_MIN_ORDER_EUR:
        return []

    # Compute actual weights per bucket
    bucket_weights: dict[str, float] = {b: 0.0 for b in BUCKETS}
    for pos in snapshot.positions:
        bucket = assign_bucket(pos, bucket_map)
        pos.bucket = bucket
        pos.weight_pct = pos.market_value / nav * 100 if nav else 0.0
        if bucket in bucket_weights:
            bucket_weights[bucket] += pos.weight_pct

    suggestions: list[DCASuggestion] = []

    for pos in snapshot.positions:
        bucket = pos.bucket
        if bucket not in BUCKETS or bucket == "CASH":
            continue

        spec = BUCKETS[bucket]
        bucket_actual = bucket_weights.get(bucket, 0.0)
        bucket_underweight = max(0.0, spec.target_pct - bucket_actual)

        # Only suggest DCA if position has dropped or bucket is underweight
        if pos.week_change_pct > -DCA_DROP_THRESHOLD_PCT and bucket_underweight < 2.0:
            continue

        # Priority: biggest dip + most underweight wins
        priority_score = (
            max(0.0, -pos.week_change_pct)        # reward bigger dips
            + bucket_underweight * 2               # reward underweight bucket
            + max(0.0, spec.target_pct - pos.weight_pct)  # reward underweight position
        )

        # Suggested order: proportional to underweight, capped at free cash
        target_value_eur = spec.target_pct / 100 * nav_eur
        current_value_eur = pos.market_value / eur_usd
        suggested = min(
            max(target_value_eur - current_value_eur, IBKR_MIN_ORDER_EUR),
            free_cash_eur * 0.5,   # don't deploy more than 50% of free cash on one position
        )
        suggested = max(suggested, IBKR_MIN_ORDER_EUR)

        # Determine DCA group
        dca_group = "A"
        for grp, tickers in DCA_GROUPS.items():
            if pos.ticker in tickers:
                dca_group = grp
                break

        reason_parts = []
        if pos.week_change_pct <= -DCA_DROP_THRESHOLD_PCT:
            reason_parts.append(f"Dip {pos.week_change_pct:.1f}%")
        if bucket_underweight >= 1.0:
            reason_parts.append(f"Bucket -{bucket_underweight:.1f}%")
        if pos.weight_pct < spec.target_pct:
            reason_parts.append(f"Position -{spec.target_pct - pos.weight_pct:.1f}%")

        suggestions.append(DCASuggestion(
            ticker=pos.ticker,
            conid=pos.conid,
            bucket=bucket,
            current_weight_pct=pos.weight_pct,
            target_weight_pct=spec.target_pct,
            underweight_pct=max(0.0, spec.target_pct - pos.weight_pct),
            week_change_pct=pos.week_change_pct,
            priority_score=priority_score,
            suggested_order_eur=suggested,
            min_order_eur=IBKR_MIN_ORDER_EUR,
            dca_group=dca_group,
            reason=" | ".join(reason_parts),
        ))

    return sorted(suggestions, key=lambda s: -s.priority_score)


def compute_week_overview(
    snapshot: IBKRSnapshot,
    bucket_map: dict[str, str],
    eur_usd: float = 1.08,
) -> WeekOverview:
    if not snapshot.positions:
        return WeekOverview()

    nav = snapshot.summary.nav
    nav_eur = nav / eur_usd if eur_usd > 0 else nav
    free_cash_eur = snapshot.free_cash / eur_usd if eur_usd > 0 else snapshot.free_cash

    bucket_weights: dict[str, float] = {b: 0.0 for b in BUCKETS}
    for pos in snapshot.positions:
        bucket = assign_bucket(pos, bucket_map)
        pos.bucket = bucket
        pos.weight_pct = pos.market_value / nav * 100 if nav else 0.0
        if bucket in bucket_weights:
            bucket_weights[bucket] += pos.weight_pct

    sorted_by_week = sorted(snapshot.positions, key=lambda p: -p.week_change_pct)
    winners = [(p.ticker, p.week_change_pct) for p in sorted_by_week[:5]]
    losers = [(p.ticker, p.week_change_pct) for p in sorted_by_week[-5:][::-1]]

    actions = []
    for bucket, actual in bucket_weights.items():
        spec = BUCKETS[bucket]
        drift = actual - spec.target_pct
        if abs(drift) > 2.0:
            direction = "Overweight" if drift > 0 else "Underweight"
            actions.append(f"{direction} {spec.label}: {actual:.1f}% vs target {spec.target_pct:.1f}%")

    cash_pct = free_cash_eur / nav_eur * 100 if nav_eur > 0 else 0.0

    return WeekOverview(
        top_winners=winners,
        top_losers=losers,
        bucket_allocations=bucket_weights,
        bucket_targets={b: BUCKETS[b].target_pct for b in BUCKETS},
        cash_pct=cash_pct,
        cash_target_pct=BUCKETS["CASH"].target_pct,
        nav=nav_eur,
        free_cash=free_cash_eur,
        action_items=actions,
    )


def compute_risk_flags(
    snapshot: IBKRSnapshot,
    bucket_map: dict[str, str],
) -> list[RiskFlag]:
    flags: list[RiskFlag] = []

    if not snapshot.positions:
        return flags

    nav = snapshot.summary.nav

    # Concentration risk
    for pos in snapshot.positions:
        weight = pos.market_value / nav * 100 if nav else 0.0
        if weight > 8.0:
            flags.append(RiskFlag(
                ticker=pos.ticker,
                flag_type="CONCENTRATION",
                description=f"{pos.ticker} is {weight:.1f}% of portfolio (>8% threshold)",
                severity="HIGH",
                color="red",
            ))

    # Bucket drift flags
    bucket_weights: dict[str, float] = {b: 0.0 for b in BUCKETS}
    for pos in snapshot.positions:
        bucket = assign_bucket(pos, bucket_map)
        weight = pos.market_value / nav * 100 if nav else 0.0
        if bucket in bucket_weights:
            bucket_weights[bucket] += weight

    for bucket, actual in bucket_weights.items():
        spec = BUCKETS[bucket]
        drift = abs(actual - spec.target_pct)
        if drift > 5.0:
            flags.append(RiskFlag(
                ticker="",
                flag_type="BUCKET_DRIFT",
                description=f"{spec.label}: {actual:.1f}% actual vs {spec.target_pct:.1f}% target (drift {drift:.1f}%)",
                severity="HIGH" if drift > 8 else "MED",
                color="red" if drift > 8 else "yellow",
            ))
        elif drift > 2.0:
            flags.append(RiskFlag(
                ticker="",
                flag_type="BUCKET_DRIFT",
                description=f"{spec.label}: {actual:.1f}% actual vs {spec.target_pct:.1f}% target (drift {drift:.1f}%)",
                severity="LOW",
                color="cyan",
            ))

    # USD exposure (positions in USD vs total)
    usd_value = sum(
        p.market_value for p in snapshot.positions if p.currency == "USD"
    )
    usd_pct = usd_value / nav * 100 if nav else 0.0
    if usd_pct > 70:
        flags.append(RiskFlag(
            ticker="",
            flag_type="FX_EXPOSURE",
            description=f"USD exposure {usd_pct:.1f}% of portfolio — high FX concentration",
            severity="MED",
            color="yellow",
        ))

    return sorted(flags, key=lambda f: {"HIGH": 0, "MED": 1, "LOW": 2}[f.severity])
