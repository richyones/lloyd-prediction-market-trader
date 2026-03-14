from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import structlog

from lloyd.common.models import Market
from lloyd.common.retry import with_retry

log = structlog.get_logger()

PAGE_LIMIT = 200


class KalshiClient:
    def __init__(
        self,
        base_url: str = "https://demo-api.kalshi.co/trade-api/v2",
        api_key_id: str = "",
        rsa_key_path: str = "",
        rsa_key_content: str = "",
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key_id = api_key_id
        self._rsa_key_path = rsa_key_path
        self._rsa_key_content = rsa_key_content
        self._private_key = self._load_rsa_key()
        self._client = http_client or httpx.AsyncClient(timeout=30.0)
        self._owns_client = http_client is None

    def _load_rsa_key(self) -> object | None:
        from cryptography.hazmat.primitives.serialization import load_pem_private_key

        # Try inline content first (Railway/cloud deployments)
        if self._rsa_key_content and self._api_key_id:
            try:
                return load_pem_private_key(
                    self._rsa_key_content.encode(), password=None
                )
            except Exception as exc:
                log.warning("kalshi_rsa_key_content_load_failed", error=str(exc))
                return None

        # Fall back to file path (local development)
        if not self._rsa_key_path or not self._api_key_id:
            return None
        key_path = Path(self._rsa_key_path)
        if not key_path.exists():
            log.warning("kalshi_rsa_key_not_found", path=self._rsa_key_path)
            return None
        try:
            return load_pem_private_key(key_path.read_bytes(), password=None)
        except Exception as exc:
            log.warning("kalshi_rsa_key_load_failed", error=str(exc))
            return None

    def _sign_request(self, method: str, path: str, timestamp_ms: int) -> dict[str, str]:
        if self._private_key is None:
            return {}
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        message = f"{timestamp_ms}{method}{path}".encode()
        signature = self._private_key.sign(  # type: ignore[union-attr]
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH,
            ),
            hashes.SHA256(),
        )
        return {
            "KALSHI-ACCESS-KEY": self._api_key_id,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
        }

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @with_retry()
    async def _fetch_page(self, cursor: str = "") -> tuple[list[dict], str]:
        path = "/trade-api/v2/markets"
        params: dict[str, str | int] = {"status": "open", "limit": PAGE_LIMIT}
        if cursor:
            params["cursor"] = cursor

        timestamp_ms = int(time.time() * 1000)
        headers = self._sign_request("GET", path, timestamp_ms)

        resp = await self._client.get(
            f"{self._base_url}/markets",
            params=params,
            headers=headers,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("markets", []), data.get("cursor", "")

    async def fetch_all_markets(self) -> list[Market]:
        t0 = time.monotonic()
        markets: list[Market] = []
        cursor = ""
        pages = 0
        parse_failures = 0

        if not self._private_key:
            log.warning("kalshi_no_auth", msg="Proceeding without authentication")

        while True:
            raw_markets, cursor = await self._fetch_page(cursor)
            pages += 1

            for raw in raw_markets:
                market = self._parse_market(raw)
                if market is not None:
                    markets.append(market)
                else:
                    parse_failures += 1

            if not cursor:
                break

        elapsed = time.monotonic() - t0
        log.info(
            "kalshi_fetch_complete",
            markets=len(markets),
            pages=pages,
            parse_failures=parse_failures,
            elapsed_seconds=round(elapsed, 2),
        )
        return markets

    def _parse_market(self, raw: dict) -> Market | None:
        try:
            ticker = raw.get("ticker")
            title = raw.get("title") or raw.get("yes_sub_title")
            if not ticker or not title:
                return None

            price = self._parse_price(raw)
            if price is None:
                return None

            close_time_str = raw.get("close_time")
            close_date = (
                datetime.fromisoformat(close_time_str) if close_time_str else None
            )

            volume_str = raw.get("volume_fp", "0")
            open_interest_str = raw.get("open_interest_fp", "0")

            return Market(
                platform="kalshi",
                platform_id=ticker,
                question=title,
                category=None,
                current_price=price,
                volume=float(volume_str),
                liquidity=None,
                open_interest=float(open_interest_str),
                close_date=close_date,
                raw_data=raw,
                fetched_at=datetime.now(timezone.utc),
            )
        except (KeyError, ValueError, TypeError) as exc:
            log.debug("kalshi_parse_skip", error=str(exc), ticker=raw.get("ticker"))
            return None

    @staticmethod
    def _parse_price(raw: dict) -> float | None:
        last = float(raw.get("last_price_dollars", "0") or "0")
        if last > 0:
            return last
        yes_bid = float(raw.get("yes_bid_dollars", "0") or "0")
        yes_ask = float(raw.get("yes_ask_dollars", "0") or "0")
        if yes_bid > 0 and yes_ask > 0:
            return (yes_bid + yes_ask) / 2
        if yes_bid > 0:
            return yes_bid
        if yes_ask > 0:
            return yes_ask
        return None
