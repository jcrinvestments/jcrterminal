"""JCR Investments Terminal — Main entry point & Rich UI."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv
from rich import box
from rich.columns import Columns
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

load_dotenv()

# Configure logging before importing modules
logging.basicConfig(
    filename="jcr_terminal.log",
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
log = logging.getLogger("jcr.main")

from config import (
    AMS_CLOSE, AMS_OPEN, BUCKETS, CASH_RESERVE_EUR,
    CONCENTRATION_ALERT_PCT, DRIFT_ALERT_PCT,
    NYSE_CLOSE, NYSE_OPEN, REFRESH,
)
from dca import (
    DCASuggestion, RiskFlag, WeekOverview,
    assign_bucket, compute_risk_flags, compute_suggestions, compute_week_overview,
)
from ibkr import IBKRSnapshot, ibkr_client
from macro import MacroDashboard, macro_fetcher
from market import MarketData, market_fetcher
from news import NewsFeed, NewsFetcher, news_fetcher

AMS_TZ = ZoneInfo("Europe/Amsterdam")
UTC_TZ = timezone.utc

console = Console()

# ---------------------------------------------------------------------------
# MCP call bridges
# ---------------------------------------------------------------------------

# These will be populated after MCP connections are established
_ibkr_mcp: Optional[Any] = None
_web_search_fn: Optional[Any] = None


async def _ibkr_call(tool: str, **kwargs: Any) -> Any:
    """Route IBKR tool calls through the MCP server."""
    if _ibkr_mcp is None:
        raise RuntimeError("IBKR MCP not connected")
    fn = getattr(_ibkr_mcp, tool, None)
    if fn is None:
        raise AttributeError(f"IBKR MCP has no tool: {tool}")
    if asyncio.iscoroutinefunction(fn):
        return await fn(**kwargs)
    return fn(**kwargs)


async def _web_search(query: str) -> Any:
    if _web_search_fn is None:
        return ""
    if asyncio.iscoroutinefunction(_web_search_fn):
        return await _web_search_fn(query)
    return _web_search_fn(query)


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
# State
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
        self.active_tab: int = 1       # F1–F4
        self.bucket_map: dict[str, str] = {}   # ticker → bucket assignment
        self.eur_usd: float = 1.08
        self._last_positions_refresh: float = 0.0
        self._last_prices_refresh: float = 0.0
        self._last_macro_refresh: float = 0.0
        self._last_news_refresh: float = 0.0

    def nav_eur(self) -> float:
        nav = self.snapshot.summary.nav
        return nav / self.eur_usd if self.eur_usd > 0 else nav

    def nav_usd(self) -> float:
        return self.snapshot.summary.nav


state = AppState()

# ---------------------------------------------------------------------------
# Rich UI builders
# ---------------------------------------------------------------------------

def _color_pct(val: float, good_positive: bool = True) -> str:
    """Return rich color string for a percentage value."""
    if val > 0:
        return "green" if good_positive else "red"
    if val < 0:
        return "red" if good_positive else "green"
    return "white"


def _fmt_pct(val: float, digits: int = 2) -> Text:
    color = _color_pct(val)
    sign = "+" if val > 0 else ""
    return Text(f"{sign}{val:.{digits}f}%", style=color)


def _fmt_num(val: float, decimals: int = 2, prefix: str = "") -> str:
    if val >= 1_000_000:
        return f"{prefix}{val/1_000_000:.2f}M"
    if val >= 1_000:
        return f"{prefix}{val/1_000:.1f}K"
    return f"{prefix}{val:.{decimals}f}"


# ---------------------------------------------------------------------------
# Status bar
# ---------------------------------------------------------------------------

def build_status_bar() -> Panel:
    now_ams = _now_ams()
    now_utc = datetime.now(UTC_TZ)
    eur_usd = state.eur_usd or state.market.eur_usd or 1.08

    nav_eur = state.nav_eur()
    nav_usd = state.nav_usd()

    ams_open = _ams_open()
    nyse_open = _nyse_open()

    last_refresh = ""
    if state.snapshot.last_updated:
        lr = state.snapshot.last_updated.astimezone(AMS_TZ)
        last_refresh = f"  Refresh: {lr.strftime('%H:%M:%S')}"

    bar = Text()
    bar.append(" JCR INVESTMENTS TERMINAL  ", style="bold white on dark_blue")
    bar.append(f" {now_ams.strftime('%a %d %b %Y  %H:%M:%S CET')} ", style="white")
    bar.append(f"| UTC {now_utc.strftime('%H:%M')} ", style="dim white")
    bar.append("| NAV ", style="white")
    bar.append(f"€{_fmt_num(nav_eur)} ", style="bold cyan")
    bar.append(f"/ ${_fmt_num(nav_usd)} ", style="cyan")
    bar.append(f"| EUR/USD {eur_usd:.4f} ", style="yellow")
    bar.append("| ")
    bar.append("AMS OPEN" if ams_open else "AMS CLOSED", style="green" if ams_open else "dim")
    bar.append("  ")
    bar.append("NYSE OPEN" if nyse_open else "NYSE CLOSED", style="green" if nyse_open else "dim")
    bar.append(last_refresh, style="dim")
    mode = getattr(state.snapshot, "mode", "demo")
    bar.append("  ")
    bar.append("● LIVE" if mode == "live" else "● DEMO", style="bold green" if mode == "live" else "bold yellow")
    if state.snapshot.is_stale:
        bar.append(" [STALE DATA]", style="bold red")

    return Panel(bar, height=3, style="on dark_blue", border_style="blue")


# ---------------------------------------------------------------------------
# Portfolio panel (left)
# ---------------------------------------------------------------------------

def build_portfolio_panel() -> Panel:
    positions = state.snapshot.positions
    if not positions:
        return Panel(
            "[dim]Waiting for IBKR data…[/]",
            title="[bold]Portfolio[/]",
            border_style="blue",
        )

    table = Table(
        box=box.SIMPLE_HEAD,
        show_header=True,
        header_style="bold white",
        expand=True,
        padding=(0, 0),
    )
    table.add_column("Ticker", style="bold", width=8)
    table.add_column("Qty", justify="right", width=7)
    table.add_column("Price", justify="right", width=9)
    table.add_column("Day%", justify="right", width=7)
    table.add_column("Wk%", justify="right", width=7)
    table.add_column("Bkt", width=5)
    table.add_column("Wt%", justify="right", width=5)
    table.add_column("Drift", justify="right", width=7)

    # Sort by bucket order then by weight
    bucket_order = {"HEDGE": 0, "LOW": 1, "MED": 2, "HIGH": 3, "CASH": 4}
    sorted_positions = sorted(
        positions,
        key=lambda p: (bucket_order.get(p.bucket, 5), -p.weight_pct),
    )

    prev_bucket = None
    nav = state.snapshot.summary.nav or 1
    for pos in sorted_positions:
        bucket = assign_bucket(pos, state.bucket_map)
        spec = BUCKETS.get(bucket)
        bucket_color = spec.color if spec else "white"

        weight = pos.market_value / nav * 100
        target = spec.target_pct if spec else 0.0
        drift = weight - target

        drift_color = "white"
        if abs(drift) > DRIFT_ALERT_PCT:
            drift_color = "orange3"

        day_txt = _fmt_pct(pos.day_change_pct)
        week_txt = _fmt_pct(pos.week_change_pct)
        drift_txt = Text(f"{drift:+.1f}%", style=drift_color)

        # Add separator between buckets
        if prev_bucket and bucket != prev_bucket:
            table.add_row("─" * 7, "", "", "", "", "", "", "", style="dim")

        table.add_row(
            Text(pos.ticker, style="bold white"),
            f"{pos.quantity:.0f}",
            f"${pos.market_price:.2f}",
            day_txt,
            week_txt,
            Text(bucket[:4], style=bucket_color),
            f"{weight:.1f}",
            drift_txt,
        )
        prev_bucket = bucket

    title = f"[bold]Portfolio[/] [dim]({len(positions)} positions)[/]"
    return Panel(table, title=title, border_style="blue", expand=True)


# ---------------------------------------------------------------------------
# Macro panel (centre)
# ---------------------------------------------------------------------------

def _sentiment_style(sentiment: str) -> str:
    return {"bullish": "green", "bearish": "red", "neutral": "white"}.get(sentiment, "white")


def build_macro_panel() -> Panel:
    indicators = state.macro.indicators

    table = Table(box=box.SIMPLE, show_header=False, expand=True, padding=(0, 1))
    table.add_column("Indicator", style="bold dim white", width=20)
    table.add_column("Value", justify="right", width=14)
    table.add_column("T", justify="center", width=2)

    indicator_order = [
        "fed_funds_rate", "fed_next_meeting_expectation",
        "us_cpi_yoy", "us_cpi_mom",
        "ecb_deposit_rate", "ecb_next_meeting_expectation",
        "us_gdp_growth", "us_unemployment",
        None,  # separator
        "us_10y_yield", "de_10y_yield",
        "eur_usd", "usd_dkk",
        None,
        "vix", "gold_spot", "wti_crude",
    ]

    for key in indicator_order:
        if key is None:
            table.add_row("", "", "", style="dim")
            continue
        ind = indicators.get(key)
        if not ind:
            # Show friendly label even when data not yet loaded
            friendly = key.replace("_", " ").title()
            table.add_row(friendly, "—", "─", style="dim")
            continue
        style = _sentiment_style(ind.sentiment)
        table.add_row(
            ind.label,
            Text(ind.format(), style=style),
            Text(ind.trend, style=style),
        )

    stale = ""
    if state.macro.is_stale:
        stale = " [red][STALE][/]"
    if state.macro.last_updated:
        t = state.macro.last_updated.astimezone(AMS_TZ).strftime("%H:%M")
        stale += f" [dim]{t}[/]"

    return Panel(table, title=f"[bold]Macro Dashboard[/]{stale}", border_style="blue", expand=True)


# ---------------------------------------------------------------------------
# News panel (right)
# ---------------------------------------------------------------------------

def build_news_panel() -> Panel:
    items = state.news.items[:20]
    if not items:
        return Panel("[dim]Loading news…[/]", title="[bold]Live News[/]", border_style="blue")

    lines = Text()
    for item in items:
        tag_str = " ".join(f"[{t}]" for t in item.tags)
        color = item.sentiment_color
        icon = item.sentiment_icon
        lines.append("● " if item.is_new else "  ", style="bold yellow" if item.is_new else "dim")
        lines.append(f"{icon} ", style=color)
        lines.append(f"{tag_str} ", style="dim cyan")
        lines.append(item.headline[:60] + ("…" if len(item.headline) > 60 else ""), style=color)
        lines.append(f"  {item.age_str()}\n", style="dim")

    stale_note = " [red][STALE][/]" if state.news.is_stale else ""
    return Panel(lines, title=f"[bold]Live News[/]{stale_note}", border_style="blue", expand=True)


# ---------------------------------------------------------------------------
# Bottom tab panels
# ---------------------------------------------------------------------------

def build_tab_f1() -> Panel:
    """DCA Suggestions."""
    suggestions = state.dca_suggestions
    if not suggestions:
        return Panel(
            "[dim]No DCA suggestions — portfolio on target or no IBKR data.[/]",
            title="[bold]F1 — DCA Suggestions[/]",
            border_style="yellow",
        )

    table = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold white")
    table.add_column("Ticker", width=8)
    table.add_column("Bucket", width=6)
    table.add_column("Wk%", justify="right", width=7)
    table.add_column("Curr%", justify="right", width=7)
    table.add_column("Tgt%", justify="right", width=6)
    table.add_column("Under%", justify="right", width=7)
    table.add_column("Order €", justify="right", width=9)
    table.add_column("Grp", justify="center", width=4)
    table.add_column("Priority", width=8)
    table.add_column("Reason", width=35)

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
            Text.from_markup(s.priority_label),
            Text(s.reason, style="dim"),
        )

    free = state.week_overview.free_cash
    info = f"[dim]Free cash: €{free:,.0f}  |  Reserve: €{CASH_RESERVE_EUR:,.0f}[/]"
    content = Table.grid(expand=True)
    content.add_row(table)
    content.add_row(Text.from_markup(info))

    return Panel(content, title="[bold]F1 — DCA Suggestions[/]", border_style="yellow", expand=True)


def build_tab_f2() -> Panel:
    """Week overview / meeting prep."""
    ov = state.week_overview
    grid = Table.grid(expand=True, padding=(0, 2))
    grid.add_column(ratio=1)
    grid.add_column(ratio=1)

    # Winners / losers
    win_table = Table(title="Top Winners (7d)", box=box.SIMPLE, header_style="bold green")
    win_table.add_column("Ticker")
    win_table.add_column("Wk%", justify="right")
    for ticker, chg in ov.top_winners:
        win_table.add_row(ticker, _fmt_pct(chg))

    lose_table = Table(title="Top Losers (7d)", box=box.SIMPLE, header_style="bold red")
    lose_table.add_column("Ticker")
    lose_table.add_column("Wk%", justify="right")
    for ticker, chg in ov.top_losers:
        lose_table.add_row(ticker, _fmt_pct(chg))

    grid.add_row(win_table, lose_table)

    # Bucket bar chart
    bar_text = Text("\nBucket Allocation\n", style="bold")
    for bucket, spec in BUCKETS.items():
        actual = ov.bucket_allocations.get(bucket, 0.0)
        target = spec.target_pct
        drift = actual - target
        bar_w = int(actual * 1.5)
        bar = "█" * bar_w
        bar_text.append(f"  {spec.label[:20]:<20} ", style="white")
        bar_text.append(f"{bar:<30}", style=spec.color)
        color = "orange3" if abs(drift) > 2 else "dim"
        bar_text.append(f" {actual:.1f}% vs {target:.1f}% ({drift:+.1f}%)\n", style=color)

    # Cash info
    bar_text.append(f"\n  NAV: €{ov.nav:,.0f}  |  Free cash: €{ov.free_cash:,.0f}  ({ov.cash_pct:.1f}% vs {ov.cash_target_pct:.0f}% target)\n",
                    style="cyan")

    # Action items
    if ov.action_items:
        bar_text.append("\nAction items:\n", style="bold yellow")
        for item in ov.action_items:
            bar_text.append(f"  • {item}\n", style="yellow")

    full = Table.grid(expand=True)
    full.add_row(grid)
    full.add_row(bar_text)

    return Panel(full, title="[bold]F2 — Week Overview / Meeting Prep[/]", border_style="green", expand=True)


def build_tab_f3() -> Panel:
    """Risk flags."""
    flags = state.risk_flags
    if not flags:
        return Panel(
            "[green]No risk flags — portfolio within parameters.[/]",
            title="[bold]F3 — Risk Flags[/]",
            border_style="green",
        )

    table = Table(box=box.SIMPLE_HEAD, expand=True, header_style="bold white")
    table.add_column("Severity", width=8)
    table.add_column("Type", width=16)
    table.add_column("Ticker", width=8)
    table.add_column("Description")

    for flag in flags:
        sev_color = {"HIGH": "red", "MED": "yellow", "LOW": "cyan"}.get(flag.severity, "white")
        table.add_row(
            Text(flag.severity, style=f"bold {sev_color}"),
            Text(flag.flag_type, style="white"),
            Text(flag.ticker or "—", style="white"),
            Text(flag.description, style=flag.color),
        )

    return Panel(table, title="[bold]F3 — Risk Flags[/]", border_style="red", expand=True)


def build_tab_f4() -> Panel:
    """Macro news detail & economic calendar."""
    cal = state.macro.economic_calendar
    news_items = [i for i in state.news.items if any(t in ["FED", "ECB", "MACRO"] for t in i.tags)]

    content = Table.grid(expand=True)

    # Economic calendar
    if cal:
        cal_table = Table(title="Economic Calendar — Next 7 Days", box=box.SIMPLE, expand=True)
        cal_table.add_column("Date", width=20)
        cal_table.add_column("Event")
        for ev in cal[:10]:
            cal_table.add_row(ev.get("date", ""), ev.get("event", ""))
        content.add_row(cal_table)

    # Macro news items
    news_text = Text("\nFed / ECB / Macro News\n", style="bold")
    for item in news_items[:15]:
        color = item.sentiment_color
        icon = item.sentiment_icon
        tags = " ".join(f"[{t}]" for t in item.tags)
        news_text.append(f"  {icon} {tags} ", style="dim cyan")
        news_text.append(f"{item.headline}\n", style=color)
        news_text.append(f"       {item.age_str()}\n", style="dim")

    content.add_row(news_text)

    return Panel(content, title="[bold]F4 — Macro News Detail[/]", border_style="cyan", expand=True)


TAB_BUILDERS = {1: build_tab_f1, 2: build_tab_f2, 3: build_tab_f3, 4: build_tab_f4}
TAB_LABELS = {1: "F1:DCA", 2: "F2:Week", 3: "F3:Risk", 4: "F4:Macro"}


def build_tab_bar() -> Text:
    txt = Text()
    for n, label in TAB_LABELS.items():
        if n == state.active_tab:
            txt.append(f" [{label}] ", style="bold white on blue")
        else:
            txt.append(f"  {label}  ", style="dim white")
        txt.append("  ")
    return txt


# ---------------------------------------------------------------------------
# Full layout
# ---------------------------------------------------------------------------

def build_layout() -> Table:
    root = Table.grid(expand=True)

    # Row 1: status bar
    root.add_row(build_status_bar())

    # Row 2: main panels (portfolio | macro | news)
    main_row = Table.grid(expand=True)
    main_row.add_column(ratio=40)
    main_row.add_column(ratio=35)
    main_row.add_column(ratio=25)
    main_row.add_row(
        build_portfolio_panel(),
        build_macro_panel(),
        build_news_panel(),
    )
    root.add_row(main_row)

    # Row 3: tab bar
    root.add_row(Panel(build_tab_bar(), height=3, border_style="dim blue"))

    # Row 4: active tab content
    tab_fn = TAB_BUILDERS.get(state.active_tab, build_tab_f1)
    root.add_row(tab_fn())

    return root


# ---------------------------------------------------------------------------
# Background refresh tasks
# ---------------------------------------------------------------------------

async def task_refresh_positions() -> None:
    while True:
        try:
            snap = await ibkr_client.refresh()
            state.snapshot = snap
            if snap.positions:
                state.eur_usd = state.market.eur_usd or state.eur_usd
                news_fetcher.set_portfolio_tickers([p.ticker for p in snap.positions])
                state.bucket_map = {p.ticker: p.bucket for p in snap.positions}
                # Recompute derived data
                state.dca_suggestions = compute_suggestions(snap, state.bucket_map, state.eur_usd)
                state.week_overview = compute_week_overview(snap, state.bucket_map, state.eur_usd)
                state.risk_flags = compute_risk_flags(snap, state.bucket_map)
        except Exception as exc:
            log.error("task_refresh_positions: %s", exc)
        interval = REFRESH["positions_market_hours"] if (_ams_open() or _nyse_open()) else REFRESH["positions_off_hours"]
        await asyncio.sleep(interval)


async def task_refresh_macro() -> None:
    while True:
        try:
            state.macro = await macro_fetcher.refresh()
        except Exception as exc:
            log.error("task_refresh_macro: %s", exc)
        await asyncio.sleep(REFRESH["macro"])


async def task_refresh_news() -> None:
    while True:
        try:
            state.news = await news_fetcher.refresh()
        except Exception as exc:
            log.error("task_refresh_news: %s", exc)
        await asyncio.sleep(REFRESH["news"])


async def task_refresh_market() -> None:
    while True:
        try:
            md = await market_fetcher.refresh()
            state.market = md
            if md.eur_usd > 0:
                state.eur_usd = md.eur_usd
        except Exception as exc:
            log.error("task_refresh_market: %s", exc)
        await asyncio.sleep(REFRESH["macro"])


# ---------------------------------------------------------------------------
# Keyboard input (non-blocking)
# ---------------------------------------------------------------------------

def _read_key_nonblocking() -> Optional[str]:
    """Read a single keypress without blocking (Unix only)."""
    try:
        import select
        import termios
        import tty
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            if select.select([sys.stdin], [], [], 0)[0]:
                ch = sys.stdin.read(1)
                if ch == "\x1b":  # escape sequence
                    rest = sys.stdin.read(2) if select.select([sys.stdin], [], [], 0.05)[0] else ""
                    return ch + rest
                return ch
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        pass
    return None


def handle_key(key: Optional[str]) -> bool:
    """Return True if should quit."""
    if not key:
        return False
    if key in ("q", "Q", "\x03"):  # q or Ctrl+C
        return True
    if key == "\x1b[A" or key == "k":  # up arrow / k
        pass
    if key == "\x1b[B" or key == "j":  # down arrow / j
        pass
    if key == "\x1b[5~":  # Page Up
        pass
    # F-keys come as escape sequences
    fkey_map = {
        "\x1bOP": 1, "\x1bOQ": 2, "\x1bOR": 3, "\x1bOS": 4,
        "\x1b[11~": 1, "\x1b[12~": 2, "\x1b[13~": 3, "\x1b[14~": 4,
    }
    if key in fkey_map:
        state.active_tab = fkey_map[key]
    # Also support 1-4 keys
    if key in ("1", "2", "3", "4"):
        state.active_tab = int(key)
    return False


# ---------------------------------------------------------------------------
# Main async entrypoint
# ---------------------------------------------------------------------------

async def main() -> None:
    global _ibkr_mcp, _web_search_fn

    console.print("[bold blue]JCR Investments Terminal[/] — Initialising…")

    # Check if a pre-populated snapshot JSON exists (written by inject_live_data.py)
    _try_load_snapshot_json()

    # IBKR: standalone Python cannot access MCP tools directly.
    # Try REST API (IBKR Gateway on localhost or OAuth token), else demo.
    from ibkr import IBKRRestClient
    rest = IBKRRestClient()
    if rest.is_configured():
        log.info("IBKR REST API configured — using live data")
        console.print("[green]IBKR REST API gevonden — live data actief[/]")
    else:
        log.warning("No IBKR REST API config found — using demo mode")
        console.print("[yellow]Geen IBKR REST API config — demo mode[/]")
        console.print("[dim]Stel IBKR_BASE_URL + IBKR_ACCESS_TOKEN in .env in voor live data[/]")
        ibkr_client.set_demo_fn(_demo_ibkr_call)

    # Web search: also not available from standalone Python.
    # Use demo search data unless overridden.
    macro_fetcher.set_web_searcher(_demo_search)
    market_fetcher.set_web_searcher(_demo_search)
    news_fetcher.set_web_searcher(_demo_search)

    console.print("[green]Starting background data tasks…[/]")

    # Start background tasks
    tasks = [
        asyncio.create_task(task_refresh_positions()),
        asyncio.create_task(task_refresh_macro()),
        asyncio.create_task(task_refresh_news()),
        asyncio.create_task(task_refresh_market()),
    ]

    console.print("[green]Launching terminal UI…[/]")
    console.print("[dim]Keys: 1-4 / F1-F4 = tabs | q = quit[/]\n")

    with Live(build_layout(), refresh_per_second=2, screen=True, console=console) as live:
        try:
            while True:
                key = _read_key_nonblocking()
                if handle_key(key):
                    break
                live.update(build_layout())
                await asyncio.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

    console.print("\n[bold]JCR Terminal closed.[/]")


# ---------------------------------------------------------------------------
# Snapshot JSON bridge — inject_live_data.py writes this, main.py reads it
# ---------------------------------------------------------------------------

SNAPSHOT_PATH = os.path.join(os.path.dirname(__file__), "data", "snapshot.json")


def _try_load_snapshot_json() -> None:
    """Load pre-fetched IBKR data from data/snapshot.json if it exists and is fresh."""
    import json
    from ibkr import AccountSummary, IBKRSnapshot, Position
    if not os.path.exists(SNAPSHOT_PATH):
        return
    try:
        with open(SNAPSHOT_PATH) as f:
            d = json.load(f)
        age = time.time() - d.get("fetched_at", 0)
        if age > 3600:  # stale after 1 hour
            log.warning("snapshot.json is %.0f minutes old — ignoring", age / 60)
            return
        s = d.get("summary", {})
        summary = AccountSummary(
            nav=s.get("nav", 0),
            nav_currency=s.get("nav_currency", "EUR"),
            cash=s.get("cash", 0),
            buying_power=s.get("buying_power", 0),
            gross_position_value=s.get("gross_position_value", 0),
            unrealized_pnl=s.get("unrealized_pnl", 0),
        )
        positions = []
        for p in d.get("positions", []):
            positions.append(Position(
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
            ))
        from ibkr import IBKRSnapshot
        snap = IBKRSnapshot(
            positions=positions,
            summary=summary,
            free_cash=d.get("free_cash", summary.cash),
            last_updated=datetime.fromtimestamp(d["fetched_at"], tz=timezone.utc),
            is_stale=False,
            mode="live",
        )
        state.snapshot = snap
        state.eur_usd = d.get("eur_usd", 1.08)
        state.bucket_map = {p.ticker: p.bucket for p in positions}
        from dca import compute_suggestions, compute_week_overview, compute_risk_flags
        state.dca_suggestions = compute_suggestions(snap, state.bucket_map, state.eur_usd)
        state.week_overview = compute_week_overview(snap, state.bucket_map, state.eur_usd)
        state.risk_flags = compute_risk_flags(snap, state.bucket_map)
        log.info("Loaded snapshot.json: %d positions, NAV=%.2f %s (%.0f min old)",
                 len(positions), summary.nav, summary.nav_currency, age / 60)
        console.print(f"[green]Snapshot geladen: {len(positions)} posities, NAV "
                      f"{summary.nav_currency} {summary.nav:,.2f} ({age/60:.0f} min oud)[/]")
    except Exception as exc:
        log.warning("Failed to load snapshot.json: %s", exc)


# ---------------------------------------------------------------------------
# Demo / fallback data for standalone mode
# ---------------------------------------------------------------------------

async def _demo_ibkr_call(tool: str, **kwargs: Any) -> Any:
    """Return realistic demo data when IBKR MCP is not connected."""
    import random
    if tool == "get_account_summary":
        return {
            "netliquidation": 285000.0,
            "totalcashvalue": 18500.0,
            "buyingpower": 18500.0,
            "grosspositionvalue": 266500.0,
            "unrealizedpnl": 12400.0,
            "realizedpnl": 3200.0,
            "maintenancemarginreq": 0.0,
            "currency": "USD",
        }
    if tool == "get_account_positions":
        demo_positions = [
            # HEDGE bucket
            {"conid": "76792991", "ticker": "GLD", "description": "SPDR Gold Shares", "position": 15,
             "mktPrice": 224.10, "mktValue": 3361.5, "avgCost": 198.40, "unrealizedPnl": 385.5,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "HEDGE"},
            # LOW bucket
            {"conid": "3691937", "ticker": "JNJ", "description": "Johnson & Johnson", "position": 40,
             "mktPrice": 157.80, "mktValue": 17532.0, "avgCost": 162.30, "unrealizedPnl": -180.0,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "LOW"},
            {"conid": "4707", "ticker": "PG", "description": "Procter & Gamble", "position": 80,
             "mktPrice": 168.20, "mktValue": 22880.0, "avgCost": 155.40, "unrealizedPnl": 1024.0,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "LOW"},
            {"conid": "5328", "ticker": "KO", "description": "Coca-Cola Co", "position": 120,
             "mktPrice": 63.40, "mktValue": 21480.0, "avgCost": 58.20, "unrealizedPnl": 624.0,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "LOW"},
            # MED bucket
            {"conid": "265598", "ticker": "AAPL", "description": "Apple Inc", "position": 65,
             "mktPrice": 189.30, "mktValue": 23104.5, "avgCost": 154.20, "unrealizedPnl": 2281.5,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "MED"},
            {"conid": "272093", "ticker": "MSFT", "description": "Microsoft Corp", "position": 35,
             "mktPrice": 415.20, "mktValue": 28064.0, "avgCost": 380.50, "unrealizedPnl": 1214.5,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "MED"},
            {"conid": "13977", "ticker": "AMZN", "description": "Amazon.com Inc", "position": 55,
             "mktPrice": 185.60, "mktValue": 20384.0, "avgCost": 138.90, "unrealizedPnl": 2568.5,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "MED"},
            {"conid": "107113386", "ticker": "ASML", "description": "ASML Holding NV", "position": 15,
             "mktPrice": 756.40, "mktValue": 17208.0, "avgCost": 698.20, "unrealizedPnl": 873.0,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "MED"},
            # HIGH bucket
            {"conid": "4815747", "ticker": "NVDA", "description": "NVIDIA Corp", "position": 30,
             "mktPrice": 875.40, "mktValue": 38262.0, "avgCost": 620.00, "unrealizedPnl": 7662.0,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "HIGH"},
            {"conid": "10375", "ticker": "META", "description": "Meta Platforms", "position": 25,
             "mktPrice": 494.30, "mktValue": 18948.75, "avgCost": 320.00, "unrealizedPnl": 4357.5,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "HIGH"},
            {"conid": "14272", "ticker": "TSLA", "description": "Tesla Inc", "position": 40,
             "mktPrice": 248.50, "mktValue": 14820.0, "avgCost": 198.00, "unrealizedPnl": 2020.0,
             "realizedPnl": 0, "currency": "USD", "assetClass": "STK", "bucket": "HIGH"},
        ]
        # Add slight random variation each call to simulate live data
        for p in demo_positions:
            p["mktPrice"] *= 1 + random.uniform(-0.002, 0.002)
            p["mktValue"] = p["mktPrice"] * p["position"]
        return demo_positions
    if tool == "get_account_balances":
        return {"availablefunds": 18500.0, "currency": "USD"}
    if tool == "get_price_snapshot":
        return []
    if tool == "get_price_history":
        # Generate 14 days of demo bars — start higher, drift down ~12% to trigger DCA
        import random
        conid = str(kwargs.get("conid", "0"))
        seed = int(conid) % 100 if conid.isdigit() else 50
        random.seed(seed)
        base = 150.0 * (1 + seed / 200)  # unique starting price per conid
        bars = []
        for i in range(14):
            drift = -0.009 if i < 10 else random.uniform(-0.01, 0.01)  # downtrend first 10 days
            base *= 1 + drift + random.uniform(-0.005, 0.005)
            bars.append({"t": int(time.time() * 1000) - (13 - i) * 86400000, "c": round(base, 2)})
        random.seed()  # reset seed
        return bars
    return {}


async def _demo_search(query: str) -> str:
    """Return placeholder data when web search is unavailable."""
    q = query.lower()
    if "fed funds" in q or "federal reserve" in q:
        return "The Federal Reserve federal funds rate is currently 5.25-5.50 percent as of 2025."
    if "cpi" in q and "year" in q:
        return "US CPI inflation year over year latest print is 3.2% for May 2025."
    if "cpi" in q and "month" in q:
        return "US CPI month over month MoM rose 0.2% in May 2025."
    if "ecb" in q:
        return "ECB deposit rate current is 3.75 percent. ECB next meeting expected to hold unchanged."
    if "gdp" in q:
        return "US GDP growth rate latest quarter 2025 annualized 2.4 percent."
    if "10 year" in q and ("treasury" in q or "us " in q):
        return "US 10-year Treasury yield today current is 4.32%."
    if "bund" in q or ("10 year" in q and "germany" in q):
        return "Germany 10 year Bund yield today current is 2.41%."
    if "eur" in q and "usd" in q:
        return "EUR/USD exchange rate current 1.0842."
    if "usd" in q and "dkk" in q:
        return "USD/DKK exchange rate current 6.8912."
    if "vix" in q:
        return "VIX fear index level today current 18.42."
    if "gold" in q:
        return "Gold spot price per ounce USD today $2,347.80."
    if "wti" in q or "crude" in q:
        return "WTI crude oil price per barrel today $78.40."
    if "unemployment" in q:
        return "US unemployment rate latest month 2025 is 3.9 percent."
    if "fomc" in q or "next meeting" in q:
        return "Fed next FOMC meeting rate cut expected. CME FedWatch shows 5.25% probability."
    if "news" in q or "market" in q or "earnings" in q:
        return (
            "Federal Reserve holds rates steady at 5.25-5.50%\n"
            "Apple beats Q2 earnings estimates, revenue up 8%\n"
            "NVIDIA announces new AI chip lineup for 2026\n"
            "ECB signals potential rate cut at September meeting\n"
            "US unemployment rate edges up to 3.9% in May\n"
            "S&P 500 reaches new all-time high above 5,300\n"
            "Meta reports strong ad revenue growth of 22% YoY\n"
            "Treasury yields rise on stronger-than-expected CPI data\n"
            "Gold rallies as inflation fears persist among investors\n"
            "ASML reports record chip equipment orders from Taiwan"
        )
    return f"[demo data] No specific result for: {query}"


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
