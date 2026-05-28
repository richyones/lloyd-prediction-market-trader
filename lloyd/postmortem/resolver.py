"""Outcome resolution — polls exchanges for settled markets, records outcomes, settles trades."""
from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

import httpx
import structlog

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
    # Trade IDs with confirmed outcomes that were never resolved because the
    # Kalshi production API is unreachable from Railway (DNS failure).
    # Populated May 27, 2026. Idempotent — no-op after first successful run.
    _KALSHI_STUCK_OVERRIDES: dict[int, str] = {
        22: "no",  # Aberg did not win PGA — Aaron Rai won
        36: "no",  # Poston did not win PGA — Aaron Rai won
        24: "no",  # Hormuz 7-day avg never exceeded 60 before May 15
        25: "no",  # Same market as 24, duplicate position
        27: "no",  # Same market as 24, duplicate position
    }

    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self._conn = conn
        self._settings = settings

    async def run(self) -> ResolverResult:
        result = ResolverResult()

        # One-off migration: settle known-stuck Kalshi trades with confirmed
        # outcomes. Idempotent — skips already-settled trades. Can be removed
        # once all 5 trades (IDs 22, 24, 25, 27, 36) show status='settled'.
        self._settle_stuck_kalshi_overrides(result)

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

        # Resolution URL is configurable (LLOYD_KALSHI_RESOLUTION_BASE_URL).
        # Defaults to demo-api.kalshi.co. Switch to api.kalshi.com if/when
        # Railway's DNS can reach the production Kalshi endpoint.
        base_url = self._settings.kalshi_resolution_base_url
        now_utc = datetime.now(timezone.utc)

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

            outcome_str: str | None = None

            try:
                resp = await client.get(
                    f"{base_url}/trade-api/v2/markets/{ticker}",
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                market_data = data.get("market", data)

                status = market_data.get("status", "").lower()
                if status == "settled":
                    result_val = market_data.get("result", "").lower()
                    if result_val == "yes":
                        outcome_str = "yes"
                    elif result_val == "no":
                        outcome_str = "no"
                    else:
                        outcome_str = "void"
            except Exception as exc:
                log.warning(
                    "kalshi_resolution_skipped",
                    market_id=market_id,
                    ticker=ticker,
                    error=str(exc),
                )
                result.errors.append(f"kalshi market {ticker}: {exc}")

            # Close-date fallback: when the API is unreachable or returns a
            # non-settled status, infer outcome from the last recorded price if
            # the market's close_date has already passed.
            #   price <= 0.05  ->  no  (YES share worth ~$0, market resolved NO)
            #   price >= 0.95  ->  yes (YES share worth ~$1, market resolved YES)
            #   else           ->  leave open (ambiguous, needs API confirmation)
            if outcome_str is None:
                close_date = self._get_market_close_date(market_id)
                if close_date is not None and close_date <= now_utc:
                    last_price = self._get_latest_price_for_platform_id(
                        "kalshi", ticker
                    )
                    if last_price is not None:
                        if last_price <= 0.05:
                            outcome_str = "no"
                        elif last_price >= 0.95:
                            outcome_str = "yes"
                        # else: ambiguous mid-range — leave open
                        if outcome_str is not None:
                            log.warning(
                                "kalshi_close_date_fallback",
                                market_id=market_id,
                                ticker=ticker,
                                last_price=last_price,
                                inferred_outcome=outcome_str,
                            )

            if outcome_str is None:
                continue

            new = self._record_outcome(market_id, "kalshi", outcome_str)
            if new:
                result.markets_resolved += 1

            settled = self._settle_trades(market_id, outcome_str)
            result.trades_settled += settled
            result.total_pnl_realized += self._sum_pnl(market_id)

    def _get_market_close_date(self, market_id: int) -> datetime | None:
        """Return close_date for a markets row as a UTC-aware datetime, or None."""
        row = self._conn.execute(
            "SELECT close_date FROM markets WHERE id = ?",
            (market_id,),
        ).fetchone()
        if not row or not row[0]:
            return None
        try:
            cd = datetime.fromisoformat(row[0])
            if cd.tzinfo is None:
                cd = cd.replace(tzinfo=timezone.utc)
            return cd
        except ValueError:
            return None

    def _get_latest_price_for_platform_id(
        self, platform: str, platform_id: str
    ) -> float | None:
        """Return the most recent current_price for a (platform, platform_id) pair."""
        row = self._conn.execute(
            """SELECT current_price FROM markets
               WHERE platform = ? AND platform_id = ?
               ORDER BY fetched_at DESC
               LIMIT 1""",
            (platform, platform_id),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def _settle_stuck_kalshi_overrides(self, result: ResolverResult) -> None:
        """One-off migration: settle Kalshi trades whose outcomes are confirmed
        but were never resolved because the production API is unreachable from
        Railway. Idempotent — silently skips already-settled trades."""
        for trade_id, outcome in self._KALSHI_STUCK_OVERRIDES.items():
            row = self._conn.execute(
                "SELECT market_id FROM trades WHERE id = ? AND status = 'open'",
                (trade_id,),
            ).fetchone()
            if row is None:
                continue  # Already settled or trade doesn't exist
            market_id: int = row[0]
            log.warning(
                "kalshi_stuck_trade_override",
                trade_id=trade_id,
                market_id=market_id,
                outcome=outcome,
            )
            new = self._record_outcome(market_id, "kalshi", outcome)
            if new:
                result.markets_resolved += 1
            settled = self._settle_trades(market_id, outcome)
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
