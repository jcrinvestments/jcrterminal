"""JCR Investments Terminal — Textual TUI (flicker-free, diff-based rendering)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    filename="jcr_terminal.log",
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("jcr.main")

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Footer, TabbedContent, TabPane

from rich import box
from rich.table import Table
from rich.text import Text

from config import (
    AMS_CLOSE, AMS_OPEN, BUCKETS, CASH_RESERVE_EUR,
    CONCENTRATION_ALERT_PCT, DRIFT_ALERT_PCT,
    NYSE_CLOSE, NYSE_OPEN, REFRESH,
)
from dca import (
    DCASuggestion, RiskFlag, WeekOverview,
    assign_bucket, compute_risk_flags, compute_suggestions, compute_week_overview,
)
from ibkr import AccountSummary, IBKRSnapshot, Position, ibkr_client
from macro import MacroDashboard, macro_fetcher
from market import MarketData, market_fetcher
from news import NewsFeed, news_fetcher

AMS_TZ = ZoneInfo("Europe/Amsterdam")
UTC_TZ = timezone.utc
SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "data", "snapshot.json")


# ---------------------------------------------------------------------------
# Market hours helpers
# ---------------------------------------------------------------------------

def _now_ams() -> datetime:
    return datetime.now(AMS_TZ)


def _market_open(open_h: int, open_m: int, close_h: int, close_m: int) -> bool:
    now = _now_ams()
    t = (now.hour, now.minute)
    return (open_h, open_m) <= t < (close_h, close_m) and now.weekday() < 5


def _ams_open() -> bool:
    return _market_open(*AMS_OPEN, *AMS_CLOSE)


def _nyse_open() -> bool:
    return _market_open(*NYSE_OPEN, *NYSE_CLOSE)


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_pct(val: float, digits: int = 2) -> Text:
    color = "green" if val > 0 else ("red" if val < 0 else "white")
    sign = "+" if val > 0 else ""
    return Text(f"{sign}{val:.{digits}f}%", style=color)


def _fmt_eur(val: float) -> str:
    """Format EUR value with thousands separator, no K/M abbreviation for small portfolios."""
    if val >= 1_000_000:
        return f"€{val/1_000_000:.2f}M"
    return f"€{val:,.0f}"


def _fmt_usd(val: float) -> str:
    if val >= 1_000_000:
        return f"${val/1_000_000:.2f}M"
    return f"${val:,.0f}"


def _sentiment_style(sentiment: str) -> str:
    return {"bullish": "green", "bearish": "red", "neutral": "white"}.get(sentiment, "white")


# ---------------------------------------------------------------------------
# Shared app state (plain object, widgets read it directly)
# ---------------------------------------------------------------------------

class AppState:
    def __init__(self) -> None:
        self.snapshot = IBKRSnapshot()
        self.macro = MacroDashboard()
        self.market = MarketData()
        self.news = NewsFeed()
        self.dca_suggestions: list[DCASuggestion] = []
        self.week_overview = WeekOverview()
        self.risk_flags: list[RiskFlag] = []
        self.bucket_map: dict[str, str] = {}
        self.eur_usd: float = 1.08

    def nav_eur(self) -> float:
        nav = self.snapshot.summary.nav
        currency = self.snapshot.summary.nav_currency
        if currency == "EUR":
            return nav
        return nav / self.eur_usd if self.eur_usd > 0 else nav

    def nav_usd(self) -> float:
        nav = self.snapshot.summary.nav
        currency = self.snapshot.summary.nav_currency
        if currency == "USD":
            return nav
        return nav * self.eur_usd


STATE = AppState()


# ---------------------------------------------------------------------------
# Snapshot JSON bridge
# ---------------------------------------------------------------------------

def _load_snapshot_json() -> bool:
    """Load pre-fetched IBKR data from data/snapshot.json. Returns True on success."""
    if not os.path.exists(SNAPSHOT_PATH):
        return False
    try:
        with open(SNAPSHOT_PATH) as f:
            d = json.load(f)
        age = time.time() - d.get("fetched_at", 0)
        if age > 7200:
            log.warning("snapshot.json is %.0f min old — skipping", age / 60)
            return False
        s = d.get("summary", {})
        summary = AccountSummary(
            nav=s.get("nav", 0),
            nav_currency=s.get("nav_currency", "EUR"),
            cash=s.get("cash", 0),
            buying_power=s.get("buying_power", 0),
            gross_position_value=s.get("gross_position_value", 0),
            unrealized_pnl=s.get("unrealized_pnl", 0),
        )
        positions = [
            Position(
                conid=p.get("conid", ""),
                ticker=p.get("ticker", ""),
                description=p.get("description", ""),
                quantity=p.get("quantity", 0),
                market_price=p.get("market_price", 0),
                market_value=p.get("market_value", 0),
                avg_cost=p.get("avg_cost", 0),
                unrealized_pnl=p.get("unrealized_pnl", 0),
                realized_pnl=p.get("realized_pnl", 0),
                daily_pnl=p.get("daily_pnl", 0),
                day_change_pct=p.get("day_change_pct", 0),
                week_change_pct=p.get("week_change_pct", 0),
                currency=p.get("currency", "USD"),
                asset_class=p.get("asset_class", "STK"),
                bucket=p.get("bucket", "MED"),
            )
            for p in d.get("positions", [])
        ]
        snap = IBKRSnapshot(
            positions=positions,
            summary=summary,
            free_cash=d.get("free_cash", summary.cash),
            last_updated=datetime.fromtimestamp(d["fetched_at"], tz=timezone.utc),
            is_stale=age > 3600,
            mode="live",
        )
        STATE.snapshot = snap
        STATE.eur_usd = d.get("eur_usd", 1.08)
        STATE.bucket_map = {p.ticker: p.bucket for p in positions}
        STATE.dca_suggestions = compute_suggestions(snap, STATE.bucket_map, STATE.eur_usd)
        STATE.week_overview = compute_week_overview(snap, STATE.bucket_map, STATE.eur_usd)
        STATE.risk_flags = compute_risk_flags(snap, STATE.bucket_map)
        log.info("Loaded snapshot.json: %d positions, NAV=%.2f %s (%.0f min old)",
                 len(positions), summary.nav, summary.nav_currency, age / 60)
        return True
    except Exception as exc:
        log.warning("Failed to load snapshot.json: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Demo data (used when no live IBKR data available)
# ---------------------------------------------------------------------------

async def _demo_search(query: str) -> str:
    q = query.lower()
    if "fed funds" in q or "federal reserve" in q:
        return "The Federal Reserve federal funds rate is currently 4.25-4.50 percent as of 2025."
    if "cpi" in q and "year" in q:
        return "US CPI inflation year over year YoY is 2.7% for May 2025."
    if "cpi" in q and "month" in q:
        return "US CPI month over month MoM rose 0.2% in May 2025."
    if "ecb" in q and "deposit" in q:
        return "ECB deposit rate is currently 2.25% in 2025."
    if "ecb" in q:
        return "ECB next meeting expected to cut rate by 0.25%."
    if "fomc" in q or "fed next" in q:
        return "Fed next FOMC meeting expectation: Hold 4.25% probability 65%."
    if "gdp" in q:
        return "US GDP growth rate Q1 2025 was 2.4% annualized."
    if "10-year" in q or "10 year" in q or "treasury" in q:
        return "US 10-year Treasury yield is currently 4.42%."
    if "bund" in q or "germany" in q:
        return "Germany 10 year Bund yield is 2.51%."
    if "eur" in q and "usd" in q:
        return "EUR/USD exchange rate current today 1.0842."
    if "usd" in q and "dkk" in q:
        return "USD/DKK exchange rate current today 6.8743."
    if "vix" in q:
        return "VIX fear index level today current 18.42."
    if "gold" in q:
        return "Gold spot price per ounce USD today $3,310.40."
    if "wti" in q or "crude" in q:
        return "WTI crude oil price per barrel today $67.80."
    if "unemployment" in q:
        return "US unemployment rate latest month 4.2% in May 2025."
    if "economic calendar" in q:
        return "June 25 2025: FOMC Minutes\nJuly 2 2025: NFP Jobs Report\nJuly 9 2025: CPI Inflation"
    return f"No data available for: {query}"


async def _demo_ibkr_call(tool: str, **kwargs: Any) -> Any:
    if tool == "get_account_summary":
        return {"netliquidation": 5442.67, "totalcashvalue": 849.39,
                "buyingpower": 849.39, "grosspositionvalue": 4593.28,
                "unrealizedpnl": -127.43, "currency": "EUR"}
    return {}


# ---------------------------------------------------------------------------
# Widgets
# ---------------------------------------------------------------------------

class StatusBar(Widget):
    """Top bar: clock, NAV, market status, mode."""

    DEFAULT_CSS = """
    StatusBar {
        height: 1;
        background: #0d1b2a;
        color: white;
    }
    """

    def on_mount(self) -> None:
        self.set_interval(1, self.refresh)

    def render(self) -> Text:
        now_ams = _now_ams()
        now_utc = datetime.now(UTC_TZ)
        nav_eur = STATE.nav_eur()
        nav_usd = STATE.nav_usd()
        eur_usd = STATE.eur_usd
        ams_open = _ams_open()
        nyse_open = _nyse_open()
        mode = getattr(STATE.snapshot, "mode", "demo")

        t = Text(overflow="ellipsis", no_wrap=True)
        t.append(" JCR INVESTMENTS ", style="bold white on #1a3a5c")
        t.append(f"  {now_ams.strftime('%a %d %b  %H:%M:%S CET')} ", style="white")
        t.append(f" UTC {now_utc.strftime('%H:%M')} ", style="dim white")
        t.append(" │ ", style="dim")
        t.append("NAV ", style="dim white")
        t.append(_fmt_eur(nav_eur), style="bold cyan")
        t.append(f"  {_fmt_usd(nav_usd)}", style="cyan")
        t.append(" │ ", style="dim")
        t.append(f"EUR/USD {eur_usd:.4f}", style="yellow")
        t.append(" │ ", style="dim")
        t.append("AMS ●" if ams_open else "AMS ○", style="bold green" if ams_open else "dim white")
        t.append("  ")
        t.append("NYSE ●" if nyse_open else "NYSE ○", style="bold green" if nyse_open else "dim white")
        t.append(" │ ", style="dim")
        if mode == "live":
            t.append("● LIVE", style="bold green")
        else:
            t.append("● DEMO", style="bold yellow")
        if STATE.snapshot.is_stale:
            t.append(" STALE", style="bold red")
        if STATE.snapshot.last_updated:
            lr = STATE.snapshot.last_updated.astimezone(AMS_TZ)
            t.append(f"  upd {lr.strftime('%H:%M')}", style="dim")
        return t


class PortfolioPanel(Widget):
    """Left panel: position list."""

    DEFAULT_CSS = """
    PortfolioPanel {
        width: 1fr;
        border: solid #1e3a5f;
        background: #080d14;
        overflow-y: auto;
    }
    """

    def on_mount(self) -> None:
        self.set_interval(30, self.refresh)

    def render(self) -> Table:
        positions = STATE.snapshot.positions
        table = Table(
            box=box.SIMPLE_HEAD,
            show_header=True,
            header_style="bold #4a9eda",
            expand=True,
            padding=(0, 0),
            title="[bold #4a9eda]PORTFOLIO[/]",
            title_style="bold #4a9eda",
        )
        table.add_column("Ticker", style="bold", width=8)
        table.add_column("Qty", justify="right", width=6)
        table.add_column("Price", justify="right", width=9)
        table.add_column("Day%", justify="right", width=7)
        table.add_column("Wk%", justify="right", width=7)
        table.add_column("Bkt", width=5)
        table.add_column("Wt%", justify="right", width=5)
        table.add_column("Drift", justify="right", width=6)

        if not positions:
            table.add_row("[dim]Loading…[/]", "", "", "", "", "", "", "")
            return table

        bucket_order = {"HEDGE": 0, "LOW": 1, "MED": 2, "HIGH": 3, "CASH": 4}
        nav = STATE.snapshot.summary.nav or 1
        sorted_pos = sorted(positions, key=lambda p: (bucket_order.get(p.bucket, 5), -p.market_value))

        prev_bucket = None
        for pos in sorted_pos:
            bucket = assign_bucket(pos, STATE.bucket_map)
            spec = BUCKETS.get(bucket)
            bkt_color = spec.color if spec else "white"
            weight = pos.market_value / nav * 100
            target = spec.target_pct if spec else 0.0
            drift = weight - target
            drift_color = "orange3" if abs(drift) > DRIFT_ALERT_PCT else "dim white"

            if prev_bucket and bucket != prev_bucket:
                table.add_row("", "", "", "", "", "", "", "", style="dim")

            table.add_row(
                Text(pos.ticker[:8], style="bold white"),
                f"{pos.quantity:.0f}",
                f"${pos.market_price:.2f}" if pos.currency == "USD" else f"€{pos.market_price:.2f}",
                _fmt_pct(pos.day_change_pct),
                _fmt_pct(pos.week_change_pct),
                Text(bucket[:4], style=bkt_color),
                f"{weight:.1f}",
                Text(f"{drift:+.1f}", style=drift_color),
            )
            prev_bucket = bucket

        return table


class MacroPanel(Widget):
    """Centre panel: macro indicators."""

    DEFAULT_CSS = """
    MacroPanel {
        width: 1fr;
        border: solid #1e3a5f;
        background: #080d14;
        overflow-y: auto;
    }
    """

    def on_mount(self) -> None:
        self.set_interval(60, self.refresh)

    def render(self) -> Table:
        indicators = STATE.macro.indicators
        table = Table(
            box=box.SIMPLE,
            show_header=False,
            expand=True,
            padding=(0, 1),
            title="[bold #4a9eda]MACRO[/]",
            title_style="bold #4a9eda",
        )
        table.add_column("Indicator", style="dim white", width=22)
        table.add_column("Value", justify="right", width=14)
        table.add_column("T", justify="center", width=2)

        order = [
            "fed_funds_rate", "fed_next_meeting_expectation",
            "us_cpi_yoy", "us_cpi_mom",
            None,
            "ecb_deposit_rate", "ecb_next_meeting_expectation",
            None,
            "us_gdp_growth", "us_unemployment",
            None,
            "us_10y_yield", "de_10y_yield",
            "eur_usd", "usd_dkk",
            None,
            "vix", "gold_spot", "wti_crude",
        ]
        for key in order:
            if key is None:
                table.add_row("", "", "")
                continue
            ind = indicators.get(key)
            if not ind:
                friendly = key.replace("_", " ").title()
                table.add_row(friendly, "—", "─", style="dim")
                continue
            style = _sentiment_style(ind.sentiment)
            table.add_row(
                ind.label,
                Text(ind.format(), style=style),
                Text(ind.trend, style=style),
            )

        if STATE.macro.last_updated:
            t = STATE.macro.last_updated.astimezone(AMS_TZ).strftime("%H:%M")
            table.caption = f"[dim]upd {t}[/]"
        return table


class NewsPanel(Widget):
    """Right panel: live news feed."""

    DEFAULT_CSS = """
    NewsPanel {
        width: 1fr;
        border: solid #1e3a5f;
        background: #080d14;
        overflow-y: auto;
    }
    """

    def on_mount(self) -> None:
        self.set_interval(30, self.refresh)

    def render(self) -> Text:
        items = STATE.news.items[:25]
        out = Text()
        out.append("  LIVE NEWS\n", style="bold #4a9eda")

        if not items:
            out.append("\n  [dim]Loading news…[/]\n")
            return out

        for item in items:
            tag_str = " ".join(f"[{t}]" for t in item.tags[:2])
            color = item.sentiment_color
            icon = item.sentiment_icon
            bullet = "●" if item.is_new else " "
            out.append(f" {bullet} ", style="bold yellow" if item.is_new else "dim")
            out.append(f"{icon} ", style=color)
            out.append(f"{tag_str} ", style="dim cyan")
            headline = item.headline
            if len(headline) > 55:
                headline = headline[:54] + "…"
            out.append(f"{headline}\n", style=color)
            out.append(f"     {item.age_str()}\n", style="dim")

        if STATE.news.last_updated:
            t = STATE.news.last_updated.astimezone(AMS_TZ).strftime("%H:%M")
            out.append(f"\n  [dim]upd {t}[/]")
        return out


class DCAPanel(Widget):
    """Tab F1: DCA suggestions."""

    DEFAULT_CSS = "DCAPanel { height: 1fr; overflow-y: auto; background: #080d14; }"

    def on_mount(self) -> None:
        self.set_interval(60, self.refresh)

    def render(self) -> Table:
        suggestions = STATE.dca_suggestions
        table = Table(
            box=box.SIMPLE_HEAD, expand=True, header_style="bold #4a9eda",
            title="[bold #4a9eda]DCA SUGGESTIONS[/]",
        )
        table.add_column("Ticker", width=8)
        table.add_column("Bucket", width=6)
        table.add_column("Wk%", justify="right", width=7)
        table.add_column("Cur%", justify="right", width=6)
        table.add_column("Tgt%", justify="right", width=6)
        table.add_column("Under%", justify="right", width=7)
        table.add_column("Order €", justify="right", width=9)
        table.add_column("Grp", justify="center", width=4)
        table.add_column("Reason", width=40)

        if not suggestions:
            table.add_row("[dim]No DCA suggestions — portfolio on target[/]", "", "", "", "", "", "", "", "")
        else:
            for s in suggestions[:15]:
                spec = BUCKETS.get(s.bucket)
                bkt_color = spec.color if spec else "white"
                table.add_row(
                    Text(s.ticker, style="bold white"),
                    Text(s.bucket[:4], style=bkt_color),
                    _fmt_pct(s.week_change_pct),
                    f"{s.current_weight_pct:.1f}",
                    f"{s.target_weight_pct:.1f}",
                    Text(f"{s.underweight_pct:.1f}", style="orange3"),
                    f"€{s.suggested_order_eur:.0f}",
                    Text(s.dca_group, style="cyan"),
                    Text(s.reason, style="dim"),
                )

        free = STATE.week_overview.free_cash
        table.caption = f"[dim]Free cash: {_fmt_eur(free)}  │  Reserve floor: {_fmt_eur(CASH_RESERVE_EUR)}[/]"
        return table


class WeekPanel(Widget):
    """Tab F2: week overview."""

    DEFAULT_CSS = "WeekPanel { height: 1fr; overflow-y: auto; background: #080d14; }"

    def on_mount(self) -> None:
        self.set_interval(60, self.refresh)

    def render(self) -> Text:
        ov = STATE.week_overview
        out = Text()

        # Winners / losers in two columns
        out.append("  TOP WINNERS (7d)                    TOP LOSERS (7d)\n", style="bold #4a9eda")
        winners = ov.top_winners[:5]
        losers = ov.top_losers[:5]
        for i in range(max(len(winners), len(losers))):
            w = f"  {winners[i][0]:<8} {'+' if winners[i][1]>0 else ''}{winners[i][1]:.2f}%" if i < len(winners) else ""
            lo = f"  {losers[i][0]:<8} {'+' if losers[i][1]>0 else ''}{losers[i][1]:.2f}%" if i < len(losers) else ""
            out.append(f"{w:<36}", style="green")
            out.append(f"{lo}\n", style="red")

        # Bucket allocation bar chart
        out.append("\n  BUCKET ALLOCATION\n", style="bold white")
        for bucket, spec in BUCKETS.items():
            actual = ov.bucket_allocations.get(bucket, 0.0)
            target = spec.target_pct
            drift = actual - target
            bar_w = max(0, int(actual * 2))
            bar = "█" * bar_w
            out.append(f"  {spec.label[:18]:<18} ", style="white")
            out.append(f"{bar:<30}", style=spec.color)
            drift_color = "orange3" if abs(drift) > 2 else "dim white"
            out.append(f" {actual:.1f}% (tgt {target:.1f}%, {drift:+.1f}%)\n", style=drift_color)

        out.append(f"\n  NAV: {_fmt_eur(ov.nav)}  │  Free cash: {_fmt_eur(ov.free_cash)} ({ov.cash_pct:.1f}%)\n",
                   style="cyan")

        if ov.action_items:
            out.append("\n  ACTION ITEMS\n", style="bold yellow")
            for item in ov.action_items:
                out.append(f"  • {item}\n", style="yellow")

        return out


class RiskPanel(Widget):
    """Tab F3: risk flags."""

    DEFAULT_CSS = "RiskPanel { height: 1fr; overflow-y: auto; background: #080d14; }"

    def on_mount(self) -> None:
        self.set_interval(60, self.refresh)

    def render(self) -> Table:
        flags = STATE.risk_flags
        table = Table(
            box=box.SIMPLE_HEAD, expand=True, header_style="bold #4a9eda",
            title="[bold #4a9eda]RISK FLAGS[/]",
        )
        table.add_column("Severity", width=8)
        table.add_column("Type", width=18)
        table.add_column("Ticker", width=8)
        table.add_column("Description")

        if not flags:
            table.add_row("", "[green]No risk flags — portfolio within parameters[/]", "", "")
        else:
            for flag in flags:
                sev_color = {"HIGH": "red", "MED": "yellow", "LOW": "cyan"}.get(flag.severity, "white")
                table.add_row(
                    Text(flag.severity, style=f"bold {sev_color}"),
                    Text(flag.flag_type, style="white"),
                    Text(flag.ticker or "—", style="white"),
                    Text(flag.description, style=flag.color),
                )
        return table


class MacroDetailPanel(Widget):
    """Tab F4: macro news detail & economic calendar."""

    DEFAULT_CSS = "MacroDetailPanel { height: 1fr; overflow-y: auto; background: #080d14; }"

    def on_mount(self) -> None:
        self.set_interval(60, self.refresh)

    def render(self) -> Text:
        cal = STATE.macro.economic_calendar
        macro_news = [i for i in STATE.news.items if any(t in ["FED", "ECB", "MACRO"] for t in i.tags)]
        out = Text()

        out.append("  ECONOMIC CALENDAR\n", style="bold #4a9eda")
        if cal:
            for ev in cal[:10]:
                out.append(f"  {ev.get('date', ''):<22}", style="yellow")
                out.append(f"{ev.get('event', '')[:80]}\n", style="white")
        else:
            out.append("  [dim]No calendar events loaded[/]\n")

        out.append("\n  FED / ECB / MACRO NEWS\n", style="bold #4a9eda")
        for item in macro_news[:20]:
            color = item.sentiment_color
            icon = item.sentiment_icon
            tags = " ".join(f"[{t}]" for t in item.tags)
            out.append(f"  {icon} {tags} ", style="dim cyan")
            out.append(f"{item.headline[:90]}\n", style=color)
            out.append(f"       {item.age_str()}\n", style="dim")

        return out


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

CSS = """
Screen {
    background: #060b11;
    layers: base overlay;
}

StatusBar {
    dock: top;
    height: 1;
}

#top-row {
    height: 1fr;
    min-height: 20;
}

PortfolioPanel {
    width: 2fr;
}

MacroPanel {
    width: 2fr;
}

NewsPanel {
    width: 2fr;
}

TabbedContent {
    height: 14;
    border: solid #1e3a5f;
    background: #080d14;
}

TabbedContent TabPane {
    padding: 0;
    background: #080d14;
}

Footer {
    background: #0d1b2a;
    color: #4a9eda;
}
"""


class JCRTerminal(App):
    """JCR Investments Terminal — Bloomberg-style TUI."""

    CSS = CSS
    TITLE = "JCR Investments Terminal"

    BINDINGS = [
        Binding("1", "switch_tab('tab-dca')", "F1:DCA"),
        Binding("2", "switch_tab('tab-week')", "F2:Week"),
        Binding("3", "switch_tab('tab-risk')", "F3:Risk"),
        Binding("4", "switch_tab('tab-macro')", "F4:Macro"),
        Binding("r", "manual_refresh", "Refresh"),
        Binding("q", "quit", "Quit"),
    ]

    def compose(self) -> ComposeResult:
        yield StatusBar()
        with Horizontal(id="top-row"):
            yield PortfolioPanel()
            yield MacroPanel()
            yield NewsPanel()
        with TabbedContent(initial="tab-dca"):
            with TabPane("F1: DCA Suggesties", id="tab-dca"):
                yield DCAPanel()
            with TabPane("F2: Week Overzicht", id="tab-week"):
                yield WeekPanel()
            with TabPane("F3: Risk Flags", id="tab-risk"):
                yield RiskPanel()
            with TabPane("F4: Macro Detail", id="tab-macro"):
                yield MacroDetailPanel()
        yield Footer()

    def on_mount(self) -> None:
        # Load snapshot immediately
        loaded = _load_snapshot_json()
        if not loaded:
            log.warning("No snapshot.json — using demo mode")

        # Wire demo search functions
        macro_fetcher.set_web_searcher(_demo_search)
        market_fetcher.set_web_searcher(_demo_search)
        news_fetcher.set_web_searcher(_demo_search)

        if STATE.snapshot.positions:
            news_fetcher.set_portfolio_tickers([p.ticker for p in STATE.snapshot.positions])

        # Schedule background refresh tasks
        self.set_interval(REFRESH["positions_off_hours"], self._bg_refresh_positions)
        self.set_interval(REFRESH["macro"], self._bg_refresh_macro)
        self.set_interval(REFRESH["news"], self._bg_refresh_news)

        # Kick off initial async refreshes after a short delay
        self.set_timer(3, self._initial_news_refresh)
        self.set_timer(5, self._initial_macro_refresh)

    async def _initial_news_refresh(self) -> None:
        await self._bg_refresh_news()

    async def _initial_macro_refresh(self) -> None:
        await self._bg_refresh_macro()

    async def _bg_refresh_positions(self) -> None:
        try:
            # Reload snapshot if a fresh one was written
            _load_snapshot_json()
            self.query_one(PortfolioPanel).refresh()
            self.query_one(DCAPanel).refresh()
            self.query_one(WeekPanel).refresh()
            self.query_one(RiskPanel).refresh()
        except Exception as exc:
            log.error("_bg_refresh_positions: %s", exc)

    async def _bg_refresh_macro(self) -> None:
        try:
            STATE.macro = await macro_fetcher.refresh()
            self.query_one(MacroPanel).refresh()
            self.query_one(MacroDetailPanel).refresh()
        except Exception as exc:
            log.error("_bg_refresh_macro: %s", exc)

    async def _bg_refresh_news(self) -> None:
        try:
            STATE.news = await news_fetcher.refresh()
            self.query_one(NewsPanel).refresh()
            self.query_one(MacroDetailPanel).refresh()
        except Exception as exc:
            log.error("_bg_refresh_news: %s", exc)

    def action_switch_tab(self, tab_id: str) -> None:
        try:
            self.query_one(TabbedContent).active = tab_id
        except Exception:
            pass

    async def action_manual_refresh(self) -> None:
        self.notify("Refreshing data…", timeout=2)
        _load_snapshot_json()
        await self._bg_refresh_macro()
        await self._bg_refresh_news()
        for w in [PortfolioPanel, MacroPanel, NewsPanel, DCAPanel, WeekPanel, RiskPanel, MacroDetailPanel]:
            try:
                self.query_one(w).refresh()
            except Exception:
                pass


if __name__ == "__main__":
    app = JCRTerminal()
    app.run()
