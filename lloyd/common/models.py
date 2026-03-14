from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


class Market(BaseModel):
    platform: Literal["polymarket", "kalshi"]
    platform_id: str
    question: str
    category: str | None = None
    current_price: float
    volume: float
    liquidity: float | None = None
    open_interest: float | None = None
    close_date: datetime | None = None
    raw_data: dict[str, Any] = {}
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ScanResult(BaseModel):
    market: Market
    exploitability_score: float
    passed_filter: bool
    scan_timestamp: datetime


class MarketPair(BaseModel):
    polymarket_market: Market
    kalshi_market: Market
    similarity_score: float
    price_divergence: float
    matched_at: datetime


# --- Stage 2 data structures ---


@dataclass
class Article:
    title: str
    source: str
    published_at: str
    snippet: str
    url: str
    sentiment_score: float | None = None


@dataclass
class NewsBundle:
    articles: list[Article] = field(default_factory=list)
    context_quality: str = "none"
    article_count: int = 0


# --- Stage 3 data structures ---


@dataclass
class PortfolioState:
    """Snapshot of the paper portfolio used by the risk sizer and paper executor."""

    cash_balance: float
    total_exposure: float
    positions: list[dict] = field(default_factory=list)

    def exposure_by_category(self, category: str) -> int:
        """Count of open positions in the given category."""
        return sum(1 for p in self.positions if p.get("category") == category)
