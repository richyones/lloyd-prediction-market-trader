"""Integration tests that hit live APIs.

Gated by the LLOYD_INTEGRATION_TESTS=1 environment variable.
These are deliberately lightweight smoke tests that verify the API
contract hasn't changed.
"""

from __future__ import annotations

import os

import pytest

from lloyd.config import Settings
from lloyd.db import get_connection, init_db, insert_markets
from lloyd.scanner.kalshi import KalshiClient
from lloyd.scanner.polymarket import PolymarketClient

requires_network = pytest.mark.skipif(
    os.environ.get("LLOYD_INTEGRATION_TESTS") != "1",
    reason="Set LLOYD_INTEGRATION_TESTS=1 to run integration tests",
)


@requires_network
@pytest.mark.asyncio
async def test_polymarket_fetch_live():
    client = PolymarketClient()
    try:
        markets = await client.fetch_all_markets()
    finally:
        await client.close()

    assert len(markets) >= 1
    m = markets[0]
    assert m.platform == "polymarket"
    assert 0.0 <= m.current_price <= 1.0
    assert m.platform_id
    assert m.question


@requires_network
@pytest.mark.asyncio
async def test_kalshi_fetch_live():
    client = KalshiClient(base_url="https://demo-api.kalshi.co/trade-api/v2")
    try:
        markets = await client.fetch_all_markets()
    finally:
        await client.close()

    assert len(markets) >= 1
    m = markets[0]
    assert m.platform == "kalshi"
    assert 0.0 <= m.current_price <= 1.0
    assert m.platform_id
    assert m.question


@requires_network
@pytest.mark.asyncio
async def test_polymarket_tags_populated():
    client = PolymarketClient()
    try:
        markets = await client.fetch_all_markets()
    finally:
        await client.close()

    categorized = [m for m in markets if m.category is not None]
    assert len(categorized) >= 1, "Expected at least some markets with category from tags"


@requires_network
@pytest.mark.asyncio
async def test_full_scan_cycle(tmp_path):
    settings = Settings(database_path=str(tmp_path / "test.db"))
    conn = get_connection(settings.database_path)
    init_db(conn)

    poly_client = PolymarketClient()
    kalshi_client = KalshiClient(base_url=settings.kalshi_base_url)

    try:
        poly_markets = await poly_client.fetch_all_markets()
        kalshi_markets = await kalshi_client.fetch_all_markets()
    finally:
        await poly_client.close()
        await kalshi_client.close()

    all_markets = poly_markets + kalshi_markets
    insert_markets(conn, all_markets)

    row_count = conn.execute("SELECT COUNT(*) FROM markets").fetchone()[0]
    assert row_count > 0

    conn.close()
