from __future__ import annotations

import httpx
import pytest

from lloyd.scanner.kalshi import KalshiClient


@pytest.mark.asyncio
async def test_fetch_single_page(mock_kalshi_response):
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_kalshi_response)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        kc = KalshiClient(http_client=client)
        markets = await kc.fetch_all_markets()

    assert len(markets) == 3
    assert all(m.platform == "kalshi" for m in markets)


@pytest.mark.asyncio
async def test_cursor_pagination(mock_kalshi_response):
    call_count = 0

    async def _handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        cursor = request.url.params.get("cursor", "")
        if not cursor:
            page1 = dict(mock_kalshi_response)
            page1["cursor"] = "next_page_token"
            return httpx.Response(200, json=page1)
        else:
            return httpx.Response(200, json={"markets": [], "cursor": ""})

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        kc = KalshiClient(http_client=client)
        markets = await kc.fetch_all_markets()

    assert call_count == 2
    assert len(markets) == 3


@pytest.mark.asyncio
async def test_price_parsing():
    data = {
        "markets": [
            {
                "ticker": "TEST-PRICE",
                "title": "Price parsing test",
                "status": "open",
                "close_time": "2026-12-31T23:59:59Z",
                "last_price_dollars": "0.5600",
                "yes_bid_dollars": "0.5500",
                "yes_ask_dollars": "0.5700",
                "volume_fp": "1000.00",
                "open_interest_fp": "500.00",
                "liquidity_dollars": "0.0000",
            }
        ],
        "cursor": "",
    }

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        kc = KalshiClient(http_client=client)
        markets = await kc.fetch_all_markets()

    assert len(markets) == 1
    assert markets[0].current_price == pytest.approx(0.56)


@pytest.mark.asyncio
async def test_price_fallback_to_midpoint():
    data = {
        "markets": [
            {
                "ticker": "TEST-MID",
                "title": "Midpoint fallback test",
                "status": "open",
                "close_time": "2026-12-31T23:59:59Z",
                "last_price_dollars": "0.0000",
                "yes_bid_dollars": "0.3000",
                "yes_ask_dollars": "0.3500",
                "volume_fp": "1000.00",
                "open_interest_fp": "500.00",
                "liquidity_dollars": "0.0000",
            }
        ],
        "cursor": "",
    }

    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=data)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        kc = KalshiClient(http_client=client)
        markets = await kc.fetch_all_markets()

    assert len(markets) == 1
    assert markets[0].current_price == pytest.approx(0.325)


@pytest.mark.asyncio
async def test_field_mapping(mock_kalshi_response):
    async def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=mock_kalshi_response)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        kc = KalshiClient(http_client=client)
        markets = await kc.fetch_all_markets()

    m = markets[0]
    assert m.platform == "kalshi"
    assert m.platform_id == "BTCUSD-26DEC31-T100000"
    assert m.question == "Bitcoin above $100,000?"
    assert m.current_price == pytest.approx(0.56)
    assert m.volume == pytest.approx(15234.0)
    assert m.liquidity is None
    assert m.open_interest == pytest.approx(8500.0)
    assert m.category is None


@pytest.mark.asyncio
async def test_no_auth_graceful(mock_kalshi_response):
    async def _handler(request: httpx.Request) -> httpx.Response:
        assert "KALSHI-ACCESS-KEY" not in request.headers
        return httpx.Response(200, json=mock_kalshi_response)

    transport = httpx.MockTransport(_handler)
    async with httpx.AsyncClient(transport=transport) as client:
        kc = KalshiClient(api_key_id="", rsa_key_path="", http_client=client)
        markets = await kc.fetch_all_markets()

    assert len(markets) > 0
