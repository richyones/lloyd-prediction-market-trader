from __future__ import annotations

from datetime import datetime, timezone

import pytest

from lloyd.common.models import Market
from lloyd.scanner.matcher import MarketMatcher


def _make_market(platform: str, question: str, price: float = 0.50) -> Market:
    return Market(
        platform=platform,
        platform_id=f"{platform}-test",
        question=question,
        current_price=price,
        volume=50_000,
        raw_data={},
        fetched_at=datetime.now(timezone.utc),
    )


class TestMatcher:
    def test_obvious_match(self):
        matcher = MarketMatcher()
        pm = [_make_market("polymarket", "Will Trump win the 2026 presidential election?", price=0.65)]
        ka = [_make_market("kalshi", "Trump 2026 presidential election winner", price=0.55)]
        pairs = matcher.match(pm, ka)
        assert len(pairs) >= 1
        assert pairs[0].similarity_score > 80

    def test_obvious_non_match(self):
        matcher = MarketMatcher()
        pm = [_make_market("polymarket", "Bitcoin price above 100k by end of year?")]
        ka = [_make_market("kalshi", "Will it rain in NYC tomorrow afternoon?")]
        pairs = matcher.match(pm, ka)
        assert len(pairs) == 0

    def test_threshold_boundary(self):
        matcher = MarketMatcher()
        # Two very similar strings that should match
        pm = [_make_market("polymarket", "Will the Federal Reserve cut interest rates in June 2026?")]
        ka = [_make_market("kalshi", "Federal Reserve interest rate cut in June 2026")]
        pairs = matcher.match(pm, ka)
        assert len(pairs) >= 1

    def test_price_divergence_calculation(self):
        matcher = MarketMatcher()
        pm = [_make_market("polymarket", "Exact same question text here for matching", price=0.65)]
        ka = [_make_market("kalshi", "Exact same question text here for matching", price=0.55)]
        pairs = matcher.match(pm, ka)
        assert len(pairs) == 1
        assert pairs[0].price_divergence == pytest.approx(0.10)

    def test_short_question_skipped(self):
        matcher = MarketMatcher()
        pm = [_make_market("polymarket", "Short?")]  # < 10 chars
        ka = [_make_market("kalshi", "Short?")]
        pairs = matcher.match(pm, ka)
        assert len(pairs) == 0

    def test_empty_input(self):
        matcher = MarketMatcher()
        assert matcher.match([], []) == []
        pm = [_make_market("polymarket", "Some long enough question here")]
        assert matcher.match(pm, []) == []
        assert matcher.match([], pm) == []

    def test_sorted_by_divergence(self):
        matcher = MarketMatcher()
        pm = [
            _make_market("polymarket", "Exact question alpha beta gamma", price=0.90),
            _make_market("polymarket", "Exact question delta epsilon zeta", price=0.50),
        ]
        ka = [
            _make_market("kalshi", "Exact question alpha beta gamma", price=0.40),
            _make_market("kalshi", "Exact question delta epsilon zeta", price=0.48),
        ]
        pairs = matcher.match(pm, ka)
        if len(pairs) >= 2:
            assert pairs[0].price_divergence >= pairs[1].price_divergence
