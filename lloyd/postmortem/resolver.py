"""Outcome resolution — polls exchanges for settled markets, records outcomes, settles trades."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import structlog

from lloyd.common.retry import with_retry
from lloyd.config import Settings

log = structlog.get_logger()

GAMMA_BASE_URL = "https://gamma-api.polymarket.com"
CLOB_BASE_URL = "https://clob.polymarket.com"


@dataclass
class ResolverResult:
    markets_resolved: int = 0
    trades_settled: int = 0
    total_pnl_realized: float = 0.0
    errors: list[str] = field(default_factory=list)


class OutcomeResolver:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self._conn = conn
        self._settings = settings

    async def run(self) -> ResolverResult:
        result = ResolverResult()

        market_ids_by_platform = self._get_open_trade_platforms()
        if not market_ids_by_platform:
            return result

        async with httpx.AsyncClient(timeout=30.0) as client:
            if "polymarket" in market_ids_by_platform:
                try:
                    await self._fetch_polymarket_resolutions(
                        client,
                        market_ids_by_platform["polymarket"],
                        result,
                    )
                except Exception as exc:
                    log.error("polymarket_resolution_failed", error=str(exc))
                    result.errors.append(f"polymarket: {exc}")

            if "kalshi" in market_ids_by_platform:
                try:
                    await self._fetch_kalshi_resolutions(
                        client,
                        market_ids_by_platform["kalshi"],
                        result,
                    )
                except Exception as exc:
                    log.error("kalshi_resolution_failed", error=str(exc))
                    result.errors.append(f"kalshi: {exc}")

        return result

    def _get_open_trade_platforms(self) -> dict[str, list[tuple[int, str]]]:
        """Return {platform: [(market_id, platform_id), ...]} for open trades."""
        rows = self._conn.execute(
            """SELECT DISTINCT t.market_id, m.platform, m.platform_id
               FROM trades t
               JOIN markets m ON m.id = t.market_id
               WHERE t.status = 'open'""",
        ).fetchall()

        by_platform: dict[str, list[tuple[int, str]]] = {}
        for market_id, platform, platform_id in rows:
            by_platform.setdefault(platform, []).append((market_id, platform_id))
        return by_platform

    async def _fetch_polymarket_resolutions(
        self,
        client: httpx.AsyncClient,
        markets: list[tuple[int, str]],
        result: ResolverResult,
    ) -> None:
        for market_id, platform_id in markets:
            try:
                resp = await client.get(f"{GAMMA_BASE_URL}/markets/{platform_id}")
                resp.raise_for_status()
            except Exception as exc:
                try:
                    clob_resp = await client.get(f"{CLOB_BASE_URL}/markets/{platform_id}")
                    clob_resp.raise_for_status()
                except Exception:
                    log.warning(
                        "polymarket_resolution_skipped",
                        market_id=market_id,
                        platform_id=platform_id,
                        error=str(exc),
                    )
                    result.errors.append(f"polymarket market {platform_id}: {exc}")
                    continue

                data = clob_resp.json()
                if not data.get("closed"):
                    continue

                tokens = data.get("tokens") or []
                winner_token = next(
                    (
                        token
                        for token in tokens
                        if isinstance(token, dict) and token.get("winner") is True
                    ),
                    None,
                )
                if winner_token is None:
                    continue

                outcome_str = str(winner_token.get("outcome", "")).lower()
                if outcome_str not in ("yes", "no"):
                    outcome_str = "void"

                log.info(
                    "polymarket_clob_resolved",
                    market_id=market_id,
                    platform_id=platform_id,
                    outcome=outcome_str,
                )
            else:
                data = resp.json()

                resolved = data.get("resolved")
                if not resolved:
                    continue

                outcome_str = data.get("outcome", "").lower()
                if outcome_str not in ("yes", "no"):
                    outcome_str = "void"

            new = self._record_outcome(market_id, "polymarket", outcome_str)
            if new:
                result.markets_resolved += 1

            settled = self._settle_trades(market_id, outcome_str)
            result.trades_settled += settled
            result.total_pnl_realized += self._sum_pnl(market_id)

    @with_retry()
    async def _fetch_kalshi_resolutions(
        self,
        client: httpx.AsyncClient,
        markets: list[tuple[int, str]],
        result: ResolverResult,
    ) -> None:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding
        from cryptography.hazmat.primitives.serialization import load_pem_private_key
        from pathlib import Path
        import base64

        private_key = None
        if self._settings.kalshi_rsa_key_path and self._settings.kalshi_api_key_id:
            key_path = Path(self._settings.kalshi_rsa_key_path)
            if key_path.exists():
                private_key = load_pem_private_key(key_path.read_bytes(), password=None)

        base_url = self._settings.kalshi_base_url.rstrip("/")

        for market_id, ticker in markets:
            path = f"/trade-api/v2/markets/{ticker}"
            timestamp_ms = int(time.time() * 1000)

            headers: dict[str, str] = {}
            if private_key is not None:
                message = f"{timestamp_ms}GET{path}".encode()
                signature = private_key.sign(  # type: ignore[union-attr]
                    message,
                    padding.PSS(
                        mgf=padding.MGF1(hashes.SHA256()),
                        salt_length=padding.PSS.MAX_LENGTH,
                    ),
                    hashes.SHA256(),
                )
                headers = {
                    "KALSHI-ACCESS-KEY": self._settings.kalshi_api_key_id,
                    "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
                    "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode(),
                }

            resp = await client.get(
                f"{base_url}/trade-api/v2/markets/{ticker}",
                headers=headers,
            )
            resp.raise_for_status()
            data = resp.json()
            market_data = data.get("market", data)

            status = market_data.get("status", "").lower()
            if status != "settled":
                continue

            result_val = market_data.get("result", "").lower()
            if result_val == "yes":
                outcome_str = "yes"
            elif result_val == "no":
                outcome_str = "no"
            else:
                outcome_str = "void"

            new = self._record_outcome(market_id, "kalshi", outcome_str)
            if new:
                result.markets_resolved += 1

            settled = self._settle_trades(market_id, outcome_str)
            result.trades_settled += settled
            result.total_pnl_realized += self._sum_pnl(market_id)

    def _record_outcome(
        self, market_id: int, platform: str, outcome: str
    ) -> bool:
        """Insert outcome row if not already present. Returns True if new."""
        existing = self._conn.execute(
            "SELECT id FROM outcomes WHERE market_id = ?",
            (market_id,),
        ).fetchone()
        if existing:
            return False

        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "INSERT INTO outcomes (market_id, platform, outcome, resolved_at) VALUES (?, ?, ?, ?)",
            (market_id, platform, outcome, now),
        )
        self._conn.commit()
        return True

    def _settle_trades(self, market_id: int, outcome: str) -> int:
        """Close all open trades for a market, computing P&L. Returns count."""
        rows = self._conn.execute(
            """SELECT id, direction, quantity, executed_price, fee
               FROM trades
               WHERE market_id = ? AND status = 'open'""",
            (market_id,),
        ).fetchall()

        now = datetime.now(timezone.utc).isoformat()
        count = 0

        for trade_id, direction, quantity, executed_price, fee in rows:
            pnl = self._calculate_pnl(direction, quantity, executed_price, fee, outcome)
            self._conn.execute(
                "UPDATE trades SET status = 'settled', pnl = ?, closed_at = ? WHERE id = ?",
                (pnl, now, trade_id),
            )
            count += 1

        self._conn.commit()
        return count

    @staticmethod
    def _calculate_pnl(
        direction: str,
        quantity: float,
        executed_price: float,
        fee: float,
        outcome: str,
    ) -> float:
        """Compute realized P&L for a settled trade.

        executed_price is the YES share price for buy_yes, NO share price for buy_no.
        A YES share pays $1 if outcome=yes, $0 if outcome=no.
        A NO share pays $1 if outcome=no, $0 if outcome=yes.
        """
        if outcome == "void":
            return 0.0

        if direction == "buy_yes":
            if outcome == "yes":
                return (1.0 - executed_price) * quantity - fee
            else:
                return -executed_price * quantity - fee
        else:
            # buy_no: executed_price is the NO share price
            if outcome == "no":
                return (1.0 - executed_price) * quantity - fee
            else:
                return -executed_price * quantity - fee

    def _sum_pnl(self, market_id: int) -> float:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(pnl), 0) FROM trades WHERE market_id = ? AND status = 'settled'",
            (market_id,),
        ).fetchone()
        return row[0] if row else 0.0
