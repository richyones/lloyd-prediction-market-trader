"""Kalshi market lookup helpers for outcome resolution."""
from __future__ import annotations

import base64
import time
from datetime import datetime, timezone
from typing import Any

import httpx
import structlog

from lloyd.config import Settings
from lloyd.scanner.kalshi import KalshiClient

log = structlog.get_logger()

# YES share thresholds for close-date price inference (matches resolver policy).
YES_PRICE_NO_MAX = 0.05
YES_PRICE_YES_MIN = 0.95


def load_kalshi_private_key(settings: Settings) -> Any | None:
    """Load RSA private key from inline content (Railway) or file path (local)."""
    from cryptography.hazmat.primitives.serialization import load_pem_private_key
    from pathlib import Path

    if settings.kalshi_rsa_key_content and settings.kalshi_api_key_id:
        try:
            return load_pem_private_key(
                settings.kalshi_rsa_key_content.encode(), password=None
            )
        except Exception as exc:
            log.warning("kalshi_rsa_key_content_load_failed", error=str(exc))

    if settings.kalshi_rsa_key_path and settings.kalshi_api_key_id:
        key_path = Path(settings.kalshi_rsa_key_path)
        if key_path.exists():
            try:
                return load_pem_private_key(key_path.read_bytes(), password=None)
            except Exception as exc:
                log.warning("kalshi_rsa_key_load_failed", error=str(exc))
    return None


def kalshi_market_request_targets(settings: Settings, ticker: str) -> list[tuple[str, str]]:
    """Return (request_url, signed_path) pairs to try, most authoritative first.

    Resolution must reflect the real world, so the production/resolution host
    is tried before the demo trading host. `fetch_kalshi_market_data` returns
    on the first HTTP 200 it gets — if the demo host were checked first and
    happened to have a market under the same ticker, its (fake, possibly
    permanently-open) data would win and the function would never even reach
    production. Demo is kept only as a last-resort fallback.
    """
    seen_urls: set[str] = set()
    targets: list[tuple[str, str]] = []
    sign_path = f"/trade-api/v2/markets/{ticker}"

    resolution_host = settings.kalshi_resolution_base_url.rstrip("/")
    if resolution_host:
        url = f"{resolution_host}/trade-api/v2/markets/{ticker}"
        if url not in seen_urls:
            targets.append((url, sign_path))
            seen_urls.add(url)

    # Hardcoded safety net in case the resolution host is misconfigured.
    # Real production base URL is external-api.kalshi.com — NOT api.kalshi.com,
    # which is not a real Kalshi hostname and never resolves. That wrong
    # hostname was previously misdiagnosed as a Railway-specific DNS outage
    # (see lloyd-backlog.md, fixed 2026-08-12).
    prod_url = f"https://external-api.kalshi.com/trade-api/v2/markets/{ticker}"
    if prod_url not in seen_urls:
        targets.append((prod_url, sign_path))
        seen_urls.add(prod_url)

    # Last resort only — the demo/paper-trading host, which may not reflect
    # the real-world outcome at all.
    trade_base = settings.kalshi_base_url.rstrip("/")
    if trade_base:
        url = f"{trade_base}/markets/{ticker}"
        if url not in seen_urls:
            targets.append((url, sign_path))
            seen_urls.add(url)

    return targets


def sign_kalshi_get(private_key: Any, api_key_id: str, sign_path: str) -> dict[str, str]:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    timestamp_ms = int(time.time() * 1000)
    message = f"{timestamp_ms}GET{sign_path}".encode()
    signature = private_key.sign(
        message,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH,
        ),
        hashes.SHA256(),
    )
    return {
        "KALSHI-ACCESS-KEY": api_key_id,
        "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
        "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
    }


async def fetch_kalshi_market_data(
    client: httpx.AsyncClient,
    settings: Settings,
    ticker: str,
    private_key: Any | None,
) -> dict[str, Any] | None:
    """Fetch market JSON from Kalshi, trying trade API then resolution/prod hosts."""
    last_exc: Exception | None = None
    for url, sign_path in kalshi_market_request_targets(settings, ticker):
        headers: dict[str, str] = {}
        if private_key is not None and settings.kalshi_api_key_id:
            headers = sign_kalshi_get(private_key, settings.kalshi_api_key_id, sign_path)
        try:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            market_data = data.get("market", data)
            if isinstance(market_data, dict):
                log.info(
                    "kalshi_market_fetched",
                    ticker=ticker,
                    url=url,
                    status=market_data.get("status"),
                    result=market_data.get("result"),
                    settlement_value=market_data.get("settlement_value_dollars"),
                )
                return market_data
        except Exception as exc:
            last_exc = exc
            log.debug(
                "kalshi_market_fetch_failed",
                ticker=ticker,
                url=url,
                error=str(exc),
            )
    if last_exc is not None:
        log.warning(
            "kalshi_resolution_skipped",
            ticker=ticker,
            error=str(last_exc),
        )
    return None


def parse_kalshi_close_time(market_data: dict[str, Any]) -> datetime | None:
    for key in ("close_time", "expiration_time"):
        raw = market_data.get(key)
        if not raw:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None


def parse_settlement_value_outcome(market_data: dict[str, Any]) -> str | None:
    """Infer yes/no from Kalshi settlement_value_dollars (authoritative post-determination)."""
    raw = market_data.get("settlement_value_dollars")
    if raw is None or raw == "":
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val >= YES_PRICE_YES_MIN:
        return "yes"
    if val <= YES_PRICE_NO_MAX:
        return "no"
    return None


def parse_settled_outcome(market_data: dict[str, Any]) -> str | None:
    """Return yes/no/void when Kalshi reports a final or determined result."""
    status = str(market_data.get("status", "")).lower()
    result_val = str(market_data.get("result", "")).lower()

    settlement_outcome = parse_settlement_value_outcome(market_data)
    if settlement_outcome is not None:
        return settlement_outcome

    # Kalshi REST uses finalized (not settled) after payout; determined while timer runs.
    terminal_statuses = {"settled", "finalized", "determined", "amended"}
    has_result = result_val in ("yes", "no", "scalar", "void")

    if status in terminal_statuses or (
        has_result and status in ("closed", "inactive")
    ):
        if result_val == "yes":
            return "yes"
        if result_val == "no":
            return "no"
        if result_val in ("scalar", "void", ""):
            if status in ("finalized", "settled") and market_data.get("settlement_ts"):
                return "void"
    return None


def infer_outcome_from_yes_price(yes_price: float) -> str | None:
    if yes_price <= YES_PRICE_NO_MAX:
        return "no"
    if yes_price >= YES_PRICE_YES_MIN:
        return "yes"
    return None


def resolve_kalshi_outcome(
    market_data: dict[str, Any] | None,
    *,
    close_date: datetime | None,
    db_yes_price: float | None,
    now_utc: datetime,
) -> tuple[str | None, str | None, dict[str, Any]]:
    """Resolve outcome preferring Kalshi API fields, then live API price, then DB price.

    Returns (outcome, resolution_method, debug_info).
    """
    debug: dict[str, Any] = {}

    if market_data:
        debug["api_status"] = market_data.get("status")
        debug["api_result"] = market_data.get("result")
        api_outcome = parse_settled_outcome(market_data)
        if api_outcome is not None:
            return api_outcome, "api_settled", debug

        api_close = parse_kalshi_close_time(market_data)
        if api_close is not None:
            close_date = api_close
        api_yes_price = KalshiClient._parse_price(market_data)
        debug["api_yes_price"] = api_yes_price

        if close_date is not None and close_date <= now_utc and api_yes_price is not None:
            inferred = infer_outcome_from_yes_price(api_yes_price)
            if inferred is not None:
                return inferred, "api_price_fallback", debug
            # API returned a live price but it is ambiguous — do not infer from stale DB.
            return None, None, debug

    debug["db_yes_price"] = db_yes_price
    if close_date is not None and close_date <= now_utc and db_yes_price is not None:
        inferred = infer_outcome_from_yes_price(db_yes_price)
        if inferred is not None:
            return inferred, "db_price_fallback", debug

    return None, None, debug
