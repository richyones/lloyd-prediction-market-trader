"""Kalshi live executor stub.

A real implementation would require:
- kalshi-python SDK or raw REST client
- RSA key authentication (KALSHI-ACCESS-KEY / SIGNATURE headers)
- Production base URL: https://trading-api.kalshi.com/trade-api/v2
- USD balance in the Kalshi account
"""
from __future__ import annotations

from lloyd.execution.base import ExecutionResult, Executor, TradeSignal


class KalshiLiveExecutor(Executor):
    """Stub — raises NotImplementedError until live trading is implemented."""

    async def execute(self, signal: TradeSignal) -> ExecutionResult:
        raise NotImplementedError(
            "Kalshi live execution not yet implemented. "
            "Set LLOYD_LIVE_TRADING_ENABLED=false."
        )

    async def get_current_price(
        self, market_id: int, platform: str, platform_id: str
    ) -> float | None:
        raise NotImplementedError(
            "Kalshi live price fetch not yet implemented. "
            "Set LLOYD_LIVE_TRADING_ENABLED=false."
        )

    def is_live(self) -> bool:
        return True
