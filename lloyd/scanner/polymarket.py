from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import httpx
import structlog

from lloyd.common.categories import normalize_category
from lloyd.common.models import Market
from lloyd.common.retry import with_retry

log = structlog.get_logger()

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
PAGE_LIMIT = 100


class PolymarketClient:
    def __init__(self, http_client: httpx.AsyncClient | None = None) -> None:
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @with_retry()
    async def _fetch_page(self, offset: int, limit: int) -> list[dict]:
        resp = await self._client.get(
            f"{GAMMA_BASE_URL}/markets",
            params={
                "closed": "false",
                "include_tag": "true",
                "limit": limit,
                "offset": offset,
            },
        )
        # Gamma API returns 422 when offset exceeds its pagination limit (~10k).
        # Treat this as end-of-results rather than a fatal error.
        if resp.status_code == 422:
            log.warning("polymarket_pagination_limit", offset=offset)
            return []
        resp.raise_for_status()
        return resp.json()

    async def fetch_all_markets(self) -> list[Market]:
        t0 = time.monotonic()
        markets: list[Market] = []
        offset = 0
        pages = 0
        parse_failures = 0

        # Gamma API hard cap is ~10k rows; stop before we hit a 422.
        MAX_OFFSET = 10000

        while offset <= MAX_OFFSET:
            raw_page = await self._fetch_page(offset, PAGE_LIMIT)
            pages += 1

            for raw in raw_page:
                market = self._parse_market(raw)
                if market is not None:
                    markets.append(market)
                else:
                    parse_failures += 1

            if len(raw_page) < PAGE_LIMIT:
                break
            offset += PAGE_LIMIT

        elapsed = time.monotonic() - t0
        log.info(
            "polymarket_fetch_complete",
            markets=len(markets),
            pages=pages,
            parse_failures=parse_failures,
            elapsed_seconds=round(elapsed, 2),
        )
        return markets

    def _parse_market(self, raw: dict) -> Market | None:
        try:
            if not raw.get("active"):
                return None

            condition_id = raw.get("conditionId")
            question = raw.get("question")
            if not condition_id or not question:
                return None

            outcome_prices_str = raw.get("outcomePrices")
            if not outcome_prices_str:
                return None

            prices = json.loads(outcome_prices_str)
            yes_price = float(prices[0])

            end_date_str = raw.get("endDateIso")
            close_date = (
                datetime.fromisoformat(end_date_str) if end_date_str else None
            )

            category = normalize_category(raw.get("tags"))

            return Market(
                platform="polymarket",
                platform_id=condition_id,
                question=question,
                category=category,
                current_price=yes_price,
                volume=float(raw.get("volumeNum") or 0),
                liquidity=float(raw.get("liquidityNum") or 0) or None,
                open_interest=None,
                close_date=close_date,
                raw_data=raw,
                fetched_at=datetime.now(timezone.utc),
            )
        except (KeyError, ValueError, IndexError, TypeError, json.JSONDecodeError) as exc:
            log.debug("polymarket_parse_skip", error=str(exc), market_id=raw.get("id"))
            return None
