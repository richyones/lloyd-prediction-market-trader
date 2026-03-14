from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from lloyd.common.models import Market
from lloyd.config import Settings
from lloyd.db import get_connection, init_db


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        min_volume=10_000,
        min_liquidity=1_000,
        min_days_to_resolution=7,
        max_days_to_resolution=90,
        database_path=":memory:",
    )


@pytest.fixture()
def tmp_db(tmp_path) -> sqlite3.Connection:
    conn = get_connection(str(tmp_path / "test.db"))
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def sample_markets() -> list[Market]:
    now = datetime.now(timezone.utc)
    base = {
        "raw_data": {},
        "fetched_at": now,
    }
    return [
        Market(
            platform="polymarket",
            platform_id="pm-1",
            question="Will Bitcoin hit 100k by end of year?",
            category="crypto",
            current_price=0.65,
            volume=50_000,
            liquidity=5_000,
            close_date=now + timedelta(days=30),
            **base,
        ),
        Market(
            platform="polymarket",
            platform_id="pm-2",
            question="Will it rain in NYC tomorrow?",
            category="weather",
            current_price=0.80,
            volume=20_000,
            liquidity=2_000,
            close_date=now + timedelta(days=14),
            **base,
        ),
        Market(
            platform="kalshi",
            platform_id="ka-1",
            question="Bitcoin above 100k?",
            category=None,
            current_price=0.55,
            volume=30_000,
            liquidity=None,
            open_interest=1_500.0,
            close_date=now + timedelta(days=30),
            **base,
        ),
        # Low volume — should be filtered
        Market(
            platform="polymarket",
            platform_id="pm-3",
            question="Some obscure market nobody trades",
            category="entertainment",
            current_price=0.50,
            volume=500,
            liquidity=100,
            close_date=now + timedelta(days=30),
            **base,
        ),
        # Extreme price — should be filtered
        Market(
            platform="polymarket",
            platform_id="pm-4",
            question="Very likely event",
            category="politics",
            current_price=0.99,
            volume=100_000,
            liquidity=10_000,
            close_date=now + timedelta(days=30),
            **base,
        ),
        # No close date — should be filtered by time filter
        Market(
            platform="kalshi",
            platform_id="ka-2",
            question="Market with no close date",
            category=None,
            current_price=0.50,
            volume=50_000,
            liquidity=None,
            close_date=None,
            **base,
        ),
    ]


@pytest.fixture()
def mock_polymarket_response() -> list[dict]:
    return [
        {
            "id": "100001",
            "conditionId": "0xabc123",
            "question": "Will BTC hit 100k by December?",
            "category": None,
            "active": True,
            "closed": False,
            "outcomePrices": '["0.85","0.15"]',
            "outcomes": '["Yes","No"]',
            "volumeNum": 250_000,
            "liquidityNum": 15_000,
            "endDateIso": "2026-12-31",
            "tags": [
                {"slug": "crypto", "label": "Crypto", "id": "21"},
            ],
        },
        {
            "id": "100002",
            "conditionId": "0xdef456",
            "question": "Will it snow in Miami?",
            "category": None,
            "active": True,
            "closed": False,
            "outcomePrices": '["0.02","0.98"]',
            "outcomes": '["Yes","No"]',
            "volumeNum": 5_000,
            "liquidityNum": 500,
            "endDateIso": "2026-06-30",
            "tags": [
                {"slug": "weather", "label": "Weather", "id": "999"},
            ],
        },
        {
            "id": "100003",
            "conditionId": "0xghi789",
            "question": "Oscar Best Picture 2027?",
            "category": None,
            "active": True,
            "closed": False,
            "outcomePrices": '["0.40","0.60"]',
            "outcomes": '["Yes","No"]',
            "volumeNum": 80_000,
            "liquidityNum": 8_000,
            "endDateIso": None,
            "tags": [
                {"slug": "pop-culture", "label": "Culture", "id": "596"},
            ],
        },
        {
            "id": "100004",
            "conditionId": "0xjkl012",
            "question": "Inactive market test",
            "category": None,
            "active": False,
            "closed": False,
            "outcomePrices": '["0.50","0.50"]',
            "outcomes": '["Yes","No"]',
            "volumeNum": 10_000,
            "liquidityNum": 1_000,
            "endDateIso": "2026-09-01",
            "tags": [],
        },
        {
            "id": "100005",
            "conditionId": "0xmno345",
            "question": "Market with null prices",
            "category": None,
            "active": True,
            "closed": False,
            "outcomePrices": None,
            "outcomes": None,
            "volumeNum": 1_000,
            "liquidityNum": 100,
            "endDateIso": "2026-08-01",
            "tags": [],
        },
    ]


@pytest.fixture()
def mock_kalshi_response() -> dict:
    return {
        "markets": [
            {
                "ticker": "BTCUSD-26DEC31-T100000",
                "title": "Bitcoin above $100,000?",
                "yes_sub_title": "Yes",
                "no_sub_title": "No",
                "status": "open",
                "close_time": "2026-12-31T23:59:59Z",
                "last_price_dollars": "0.5600",
                "yes_bid_dollars": "0.5500",
                "yes_ask_dollars": "0.5700",
                "no_bid_dollars": "0.4300",
                "no_ask_dollars": "0.4500",
                "volume_fp": "15234.00",
                "open_interest_fp": "8500.00",
                "liquidity_dollars": "0.0000",
            },
            {
                "ticker": "RAIN-NYC-26MAR14",
                "title": "Rain in NYC on March 14?",
                "yes_sub_title": "Yes",
                "no_sub_title": "No",
                "status": "open",
                "close_time": "2026-03-14T23:59:59Z",
                "last_price_dollars": "0.0000",
                "yes_bid_dollars": "0.3000",
                "yes_ask_dollars": "0.3500",
                "no_bid_dollars": "0.6500",
                "no_ask_dollars": "0.7000",
                "volume_fp": "500.00",
                "open_interest_fp": "200.00",
                "liquidity_dollars": "0.0000",
            },
            {
                "ticker": "OSCARS-27-BP",
                "title": "Oscar Best Picture 2027?",
                "yes_sub_title": "Yes",
                "no_sub_title": "No",
                "status": "open",
                "close_time": "2027-03-01T23:59:59Z",
                "last_price_dollars": "0.4200",
                "yes_bid_dollars": "0.4100",
                "yes_ask_dollars": "0.4300",
                "no_bid_dollars": "0.5700",
                "no_ask_dollars": "0.5900",
                "volume_fp": "45000.00",
                "open_interest_fp": "12000.00",
                "liquidity_dollars": "0.0000",
            },
        ],
        "cursor": "",
    }
