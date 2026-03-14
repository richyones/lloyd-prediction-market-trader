from __future__ import annotations

import httpx
import pytest

from lloyd.scanner.polymarket import PolymarketClient


@pytest.mark.asyncio
async def test_fetch_single_page(mock_polymarket_response):
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_polymarket_response)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pc = PolymarketClient(http_client=client)
        markets = await pc.fetch_all_markets()

    # 5 raw, but 1 inactive + 1 null prices = 3 parsed
    assert len(markets) == 3
    assert all(m.platform == "polymarket" for m in markets)


@pytest.mark.asyncio
async def test_fetch_pagination(mock_polymarket_response):
    call_count = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        offset = int(request.url.params.get("offset", "0"))
        if offset == 0:
            # Return exactly PAGE_LIMIT items to trigger next page
            full_page = mock_polymarket_response * 20  # 100 items
            return httpx.Response(200, json=full_page)
        else:
            return httpx.Response(200, json=mock_polymarket_response[:2])

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pc = PolymarketClient(http_client=client)
        markets = await pc.fetch_all_markets()

    assert call_count == 2
    assert len(markets) > 0


@pytest.mark.asyncio
async def test_outcome_prices_double_parse():
    raw = [
        {
            "id": "1",
            "conditionId": "0xtest",
            "question": "Double parse test?",
            "active": True,
            "outcomePrices": '["0.85","0.15"]',
            "volumeNum": 1000,
            "liquidityNum": 100,
            "endDateIso": "2026-12-31",
            "tags": [],
        }
    ]

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pc = PolymarketClient(http_client=client)
        markets = await pc.fetch_all_markets()

    assert len(markets) == 1
    assert markets[0].current_price == pytest.approx(0.85)


@pytest.mark.asyncio
async def test_missing_outcome_prices():
    raw = [
        {
            "id": "1",
            "conditionId": "0xtest",
            "question": "No prices?",
            "active": True,
            "outcomePrices": None,
            "volumeNum": 1000,
            "liquidityNum": 100,
            "endDateIso": "2026-12-31",
            "tags": [],
        }
    ]

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pc = PolymarketClient(http_client=client)
        markets = await pc.fetch_all_markets()

    assert len(markets) == 0


@pytest.mark.asyncio
async def test_retry_on_429(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("lloyd.common.retry.asyncio.sleep", AsyncMock())

    attempt = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempt
        attempt += 1
        if attempt <= 2:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200,
            json=[
                {
                    "id": "1",
                    "conditionId": "0xretry",
                    "question": "Retry test?",
                    "active": True,
                    "outcomePrices": '["0.50","0.50"]',
                    "volumeNum": 1000,
                    "liquidityNum": 100,
                    "endDateIso": "2026-12-31",
                    "tags": [],
                }
            ],
        )

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pc = PolymarketClient(http_client=client)
        markets = await pc.fetch_all_markets()

    assert attempt == 3
    assert len(markets) == 1


@pytest.mark.asyncio
async def test_field_mapping():
    raw = [
        {
            "id": "42",
            "conditionId": "0xmap_test",
            "question": "Field mapping verification?",
            "active": True,
            "outcomePrices": '["0.72","0.28"]',
            "volumeNum": 99999.5,
            "liquidityNum": 5432.1,
            "endDateIso": "2026-06-15",
            "tags": [],
        }
    ]

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pc = PolymarketClient(http_client=client)
        markets = await pc.fetch_all_markets()

    m = markets[0]
    assert m.platform == "polymarket"
    assert m.platform_id == "0xmap_test"
    assert m.question == "Field mapping verification?"
    assert m.current_price == pytest.approx(0.72)
    assert m.volume == pytest.approx(99999.5)
    assert m.liquidity == pytest.approx(5432.1)


@pytest.mark.asyncio
async def test_inactive_market_filtered():
    raw = [
        {
            "id": "1",
            "conditionId": "0xinactive",
            "question": "Inactive?",
            "active": False,
            "outcomePrices": '["0.50","0.50"]',
            "volumeNum": 1000,
            "liquidityNum": 100,
            "endDateIso": "2026-12-31",
            "tags": [],
        }
    ]

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pc = PolymarketClient(http_client=client)
        markets = await pc.fetch_all_markets()

    assert len(markets) == 0


@pytest.mark.asyncio
async def test_category_from_tags():
    raw = [
        {
            "id": "1",
            "conditionId": "0xcategory",
            "question": "Category test?",
            "active": True,
            "outcomePrices": '["0.50","0.50"]',
            "volumeNum": 1000,
            "liquidityNum": 100,
            "endDateIso": "2026-12-31",
            "tags": [{"slug": "pop-culture", "label": "Culture", "id": "596"}],
        }
    ]

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=raw)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        pc = PolymarketClient(http_client=client)
        markets = await pc.fetch_all_markets()

    assert len(markets) == 1
    assert markets[0].category == "entertainment"
