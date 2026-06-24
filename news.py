"""Live news feed for JCR Terminal."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config import NEWS_TAGS, NEWS_PRIORITY_KEYWORDS

log = logging.getLogger("jcr.news")

SENTIMENT_WORDS = {
    "bullish": ["rally", "surge", "beat", "exceeds", "strong", "growth", "recovery",
                "record high", "upgrade", "bullish", "positive", "gain", "rises",
                "cuts rate", "rate cut", "stimulus", "buyback", "dividend"],
    "bearish": ["crash", "miss", "below", "weak", "decline", "recession", "layoffs",
                "downgrade", "bearish", "loss", "falls", "hike", "rate hike",
                "default", "bankruptcy", "warning", "concern", "risk"],
}


@dataclass
class NewsItem:
    headline: str
    source: str = ""
    url: str = ""
    tags: list[str] = field(default_factory=list)
    sentiment: str = "neutral"  # bull / bear / neutral
    priority: int = 0           # higher = more relevant to JCR
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    is_new: bool = True

    @property
    def sentiment_icon(self) -> str:
        return {"bull": "▲", "bear": "▼", "neutral": "─"}.get(self.sentiment, "─")

    @property
    def sentiment_color(self) -> str:
        return {"bull": "green", "bear": "red", "neutral": "white"}.get(self.sentiment, "white")

    def age_str(self) -> str:
        delta = datetime.now(timezone.utc) - self.timestamp
        mins = int(delta.total_seconds() / 60)
        if mins < 60:
            return f"{mins}m ago"
        hours = mins // 60
        if hours < 24:
            return f"{hours}h ago"
        return f"{hours // 24}d ago"


@dataclass
class NewsFeed:
    items: list[NewsItem] = field(default_factory=list)
    last_updated: Optional[datetime] = None
    is_stale: bool = False


def _classify_tags(headline: str) -> list[str]:
    tags = []
    for tag, keywords in NEWS_TAGS.items():
        if any(kw.lower() in headline.lower() for kw in keywords):
            tags.append(tag)
    return tags or ["GENERAL"]


def _classify_sentiment(headline: str) -> str:
    hl = headline.lower()
    bull_score = sum(1 for w in SENTIMENT_WORDS["bullish"] if w in hl)
    bear_score = sum(1 for w in SENTIMENT_WORDS["bearish"] if w in hl)
    if bull_score > bear_score:
        return "bull"
    if bear_score > bull_score:
        return "bear"
    return "neutral"


def _priority(headline: str, tickers: list[str]) -> int:
    score = 0
    hl = headline.lower()
    # Direct ticker mentions
    for t in tickers:
        if t.lower() in hl:
            score += 10
    # Priority keywords
    for kw in NEWS_PRIORITY_KEYWORDS:
        if kw.lower() in hl:
            score += 2
    return score


class NewsFetcher:
    MAX_ITEMS = 50

    def __init__(self) -> None:
        self._feed = NewsFeed()
        self._web_search: Optional[object] = None
        self._lock = asyncio.Lock()
        self._seen_headlines: set[str] = set()
        self._first_seen: dict[str, datetime] = {}
        self._portfolio_tickers: list[str] = []

    def set_web_searcher(self, fn: object) -> None:
        self._web_search = fn

    def set_portfolio_tickers(self, tickers: list[str]) -> None:
        self._portfolio_tickers = tickers

    async def _search(self, query: str) -> str:
        if self._web_search is None:
            return ""
        try:
            result = await self._web_search(query)
            if isinstance(result, list):
                return " ".join(str(r) for r in result)
            return str(result)
        except Exception as exc:
            log.warning("News search '%s' failed: %s", query, exc)
            return ""

    def _parse_items(self, text: str, source_tag: str) -> list[NewsItem]:
        items = []
        # Split on newlines and bullet points
        for line in re.split(r"\n|•|·|\d+\.\s", text):
            line = line.strip()
            if len(line) < 20:
                continue
            # Skip if it looks like a URL or navigation text
            if line.startswith("http") or len(line) > 300:
                continue
            # Basic headline detection: starts uppercase, has real words
            if not re.match(r"[A-Z\$€£]", line):
                continue
            headline = line[:200]
            if headline in self._seen_headlines:
                continue
            tags = _classify_tags(headline)
            sentiment = _classify_sentiment(headline)
            priority = _priority(headline, self._portfolio_tickers)
            now = datetime.now(timezone.utc)
            if headline not in self._first_seen:
                self._first_seen[headline] = now
            items.append(NewsItem(
                headline=headline,
                source=source_tag,
                tags=tags,
                sentiment=sentiment,
                priority=priority,
                timestamp=self._first_seen[headline],
                is_new=True,
            ))
            self._seen_headlines.add(headline)
        return items

    async def refresh(self) -> NewsFeed:
        async with self._lock:
            ticker_str = " ".join(self._portfolio_tickers[:10])
            queries = [
                ("Federal Reserve FOMC news today 2025", "FED"),
                ("ECB European Central Bank news today 2025", "ECB"),
                (f"stock market earnings news today {ticker_str}", "EARNINGS"),
                ("US economic data macro news today 2025", "MACRO"),
                ("financial markets news today", "MARKET"),
            ]
            all_items: list[NewsItem] = []
            for query, tag in queries:
                text = await self._search(query)
                items = self._parse_items(text, tag)
                all_items.extend(items)
                await asyncio.sleep(0.2)

            # Merge with existing, mark old items
            existing = {item.headline: item for item in self._feed.items}
            for item in all_items:
                item.is_new = item.headline not in existing

            # Combine: new items first (by priority), then old
            combined = sorted(all_items, key=lambda x: (-x.priority, -x.is_new))
            # Append remaining old items not seen in new batch
            new_headlines = {i.headline for i in all_items}
            for old_item in self._feed.items:
                if old_item.headline not in new_headlines:
                    old_item.is_new = False
                    combined.append(old_item)

            self._feed = NewsFeed(
                items=combined[: self.MAX_ITEMS],
                last_updated=datetime.now(timezone.utc),
                is_stale=False,
            )
        return self._feed

    @property
    def feed(self) -> NewsFeed:
        return self._feed


# Singleton
news_fetcher = NewsFetcher()
