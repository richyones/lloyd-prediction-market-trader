from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from lloyd.common.categories import TAG_SLUG_TO_CATEGORY, normalize_category
from lloyd.common.models import Market
from lloyd.scanner.scanner import CATEGORY_MULTIPLIERS, DEFAULT_MULTIPLIER, MarketScanner


def _make_market(**overrides) -> Market:
    now = datetime.now(timezone.utc)
    defaults = {
        "platform": "polymarket",
        "platform_id": "test-id",
        "question": "Test market?",
        "category": "crypto",
        "current_price": 0.50,
        "volume": 50_000,
        "liquidity": 5_000,
        "close_date": now + timedelta(days=30),
        "raw_data": {},
        "fetched_at": now,
    }
    defaults.update(overrides)
    return Market(**defaults)


class TestVolumeFilter:
    def test_at_threshold_fails(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(volume=10_000)
        assert scanner._filter_volume([m]) == []

    def test_above_threshold_passes(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(volume=10_001)
        assert len(scanner._filter_volume([m])) == 1


class TestTimeFilter:
    def test_too_soon_fails(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(close_date=datetime.now(timezone.utc) + timedelta(days=6, hours=23))
        assert scanner._filter_time([m]) == []

    def test_min_boundary_passes(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(close_date=datetime.now(timezone.utc) + timedelta(days=7, hours=1))
        assert len(scanner._filter_time([m])) == 1

    def test_max_boundary_passes(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(close_date=datetime.now(timezone.utc) + timedelta(days=89, hours=23))
        assert len(scanner._filter_time([m])) == 1

    def test_too_far_fails(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(close_date=datetime.now(timezone.utc) + timedelta(days=91))
        assert scanner._filter_time([m]) == []

    def test_no_close_date_fails(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(close_date=None)
        assert scanner._filter_time([m]) == []


class TestPriceFilter:
    def test_at_lower_boundary_fails(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(current_price=0.05)
        assert scanner._filter_price([m]) == []

    def test_above_lower_boundary_passes(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(current_price=0.06)
        assert len(scanner._filter_price([m])) == 1

    def test_below_upper_boundary_passes(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(current_price=0.94)
        assert len(scanner._filter_price([m])) == 1

    def test_at_upper_boundary_fails(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(current_price=0.95)
        assert scanner._filter_price([m]) == []


class TestLiquidityFilter:
    def test_none_passes(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(liquidity=None)
        assert len(scanner._filter_liquidity([m])) == 1

    def test_below_threshold_fails(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(liquidity=999)
        assert scanner._filter_liquidity([m]) == []

    def test_above_threshold_passes(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(liquidity=1_001)
        assert len(scanner._filter_liquidity([m])) == 1


class TestScoring:
    def test_category_multiplier(self, settings):
        scanner = MarketScanner(settings)
        ent = _make_market(category="entertainment", volume=50_000, current_price=0.50)
        fin = _make_market(category="finance", volume=50_000, current_price=0.50)
        score_ent = scanner._score(ent)
        score_fin = scanner._score(fin)
        assert score_ent / score_fin == pytest.approx(
            CATEGORY_MULTIPLIERS["entertainment"] / CATEGORY_MULTIPLIERS["finance"]
        )

    def test_extreme_price_bonus(self, settings):
        scanner = MarketScanner(settings)
        extreme = _make_market(current_price=0.10, volume=50_000)
        moderate = _make_market(current_price=0.45, volume=50_000)
        assert scanner._score(extreme) > scanner._score(moderate)

    def test_none_category_uses_default(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(category=None, volume=50_000, current_price=0.50)
        score = scanner._score(m)
        m_explicit = _make_market(category="unknown_xyz", volume=50_000, current_price=0.50)
        score_explicit = scanner._score(m_explicit)
        assert score == pytest.approx(score_explicit)


class TestScan:
    def test_empty_input(self, settings):
        scanner = MarketScanner(settings)
        assert scanner.scan([]) == []

    def test_all_filtered_out(self, settings):
        scanner = MarketScanner(settings)
        m = _make_market(volume=1)  # fails volume filter
        assert scanner.scan([m]) == []


class TestNormalizeCategory:
    def test_known_mappings(self):
        for slug, expected in TAG_SLUG_TO_CATEGORY.items():
            tags = [{"slug": slug, "label": slug}]
            assert normalize_category(tags) == expected, f"Failed for slug={slug}"

    def test_no_match_returns_none(self):
        tags = [{"slug": "totally-unknown", "label": "???"}]
        assert normalize_category(tags) is None

    def test_no_match_returns_fallback(self):
        tags = [{"slug": "totally-unknown", "label": "???"}]
        assert normalize_category(tags, fallback="other") == "other"

    def test_empty_tags(self):
        assert normalize_category([]) is None
        assert normalize_category(None) is None

    def test_first_match_wins(self):
        tags = [
            {"slug": "pop-culture", "label": "Culture"},
            {"slug": "politics", "label": "Politics"},
        ]
        assert normalize_category(tags) == "entertainment"
