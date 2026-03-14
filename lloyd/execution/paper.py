from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import structlog

from lloyd.common.models import PortfolioState
from lloyd.config import Settings, get_settings
from lloyd.db import (
    get_open_paper_trades,
    insert_portfolio_snapshot,
    insert_trade,
)
from lloyd.execution.base import ExecutionResult, Executor, TradeSignal
from lloyd.scanner.kalshi import KalshiClient
from lloyd.scanner.polymarket import GAMMA_BASE_URL, PolymarketClient

log = structlog.get_logger()


class PaperExecutor(Executor):
    """Simulates order execution with realistic latency, slippage, and fees."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings | None = None,
        polymarket_client: PolymarketClient | None = None,
        kalshi_client: KalshiClient | None = None,
    ) -> None:
        self._conn = conn
        self._s = settings or get_settings()
        self._poly = polymarket_client or PolymarketClient()
        self._kalshi = kalshi_client or KalshiClient(
            base_url=self._s.kalshi_base_url,
            api_key_id=self._s.kalshi_api_key_id,
            rsa_key_path=self._s.kalshi_rsa_key_path,
        )

    async def execute(self, signal: TradeSignal) -> ExecutionResult:
        await asyncio.sleep(0.3)

        executed_price, slippage, fee = self._simulate_execution(signal)
        now = datetime.now(timezone.utc).isoformat()

        trade_id = insert_trade(
            self._conn,
            market_id=signal.market_id,
            ensemble_prediction_id=signal.ensemble_prediction_id,
            platform=signal.platform,
            direction=signal.direction,
            quantity=signal.quantity,
            limit_price=signal.limit_price,
            executed_price=executed_price,
            slippage=slippage,
            fee=fee,
            is_paper=True,
            status="open",
            opened_at=now,
        )

        return ExecutionResult(
            trade_id=trade_id,
            market_id=signal.market_id,
            platform=signal.platform,
            direction=signal.direction,
            quantity=signal.quantity,
            limit_price=signal.limit_price,
            executed_price=executed_price,
            slippage=slippage,
            fee=fee,
            is_paper=True,
            status="open",
            opened_at=now,
        )

    async def get_current_price(
        self, market_id: int, platform: str, platform_id: str
    ) -> float | None:
        try:
            if platform == "polymarket":
                resp = await self._poly._client.get(
                    f"{GAMMA_BASE_URL}/markets/{platform_id}",
                )
                resp.raise_for_status()
                data = resp.json()
                outcome_prices = data.get("outcomePrices")
                if outcome_prices:
                    prices = json.loads(outcome_prices) if isinstance(outcome_prices, str) else outcome_prices
                    return float(prices[0])
                return None

            if platform == "kalshi":
                import time

                path = f"/trade-api/v2/markets/{platform_id}"
                timestamp_ms = int(time.time() * 1000)
                headers = self._kalshi._sign_request("GET", path, timestamp_ms)
                resp = await self._kalshi._client.get(
                    f"{self._kalshi._base_url}/markets/{platform_id}",
                    headers=headers,
                )
                resp.raise_for_status()
                data = resp.json()
                market_data = data.get("market", data)
                return KalshiClient._parse_price(market_data)

            return None
        except Exception as exc:
            log.debug(
                "price_fetch_failed",
                platform=platform,
                platform_id=platform_id,
                error=str(exc),
            )
            return None

    def is_live(self) -> bool:
        return False

    def get_portfolio_state(self) -> PortfolioState:
        """Build current portfolio state from open paper trades."""
        trades = get_open_paper_trades(self._conn)
        total_exposure = sum(t.quantity * t.executed_price for t in trades)
        cash_balance = self._s.paper_bankroll - total_exposure

        positions = [
            {
                "trade_id": t.id,
                "market_id": t.market_id,
                "platform": t.platform,
                "direction": t.direction,
                "quantity": t.quantity,
                "entry_price": t.executed_price,
                "category": t.category,
            }
            for t in trades
        ]

        return PortfolioState(
            cash_balance=cash_balance,
            total_exposure=total_exposure,
            positions=positions,
        )

    def snapshot_portfolio(
        self, unrealized_prices: dict[int, float] | None = None
    ) -> None:
        """Write a portfolio snapshot row. Pass trade_id -> current_price for unrealized P&L."""
        state = self.get_portfolio_state()

        unrealized_pnl: float | None = None
        if unrealized_prices is not None:
            unrealized_pnl = 0.0
            for pos in state.positions:
                tid = pos["trade_id"]
                current = unrealized_prices.get(tid)
                if current is None:
                    continue
                entry = pos["entry_price"]
                qty = pos["quantity"]
                if pos["direction"] == "buy_yes":
                    unrealized_pnl += (current - entry) * qty
                else:
                    unrealized_pnl += (entry - current) * qty

        snapshot_json = json.dumps(state.positions)

        insert_portfolio_snapshot(
            self._conn,
            timestamp=datetime.now(timezone.utc).isoformat(),
            cash_balance=state.cash_balance,
            total_exposure=state.total_exposure,
            unrealized_pnl=unrealized_pnl,
            realized_pnl=None,
            num_open_positions=len(state.positions),
            snapshot=snapshot_json,
        )

    def _simulate_execution(
        self, signal: TradeSignal
    ) -> tuple[float, float, float]:
        slippage_direction = 1 if signal.direction == "buy_yes" else -1
        slippage = slippage_direction * signal.limit_price * self._s.paper_slippage_pct
        executed_price = signal.limit_price + slippage
        fee = self._calculate_fee(signal.platform, signal.quantity, executed_price)
        return executed_price, slippage, fee

    def _calculate_fee(self, platform: str, quantity: float, price: float) -> float:
        if platform == "polymarket":
            return self._s.polymarket_fee_rate * quantity * price
        if platform == "kalshi":
            return self._s.kalshi_fee_rate * quantity * min(price, 1 - price)
        return 0.0
