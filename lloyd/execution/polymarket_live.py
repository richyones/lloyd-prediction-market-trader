"""Polymarket live executor stub.

A real implementation would require:
- py-clob-client for CLOB order placement
- EIP-712 signing with a wallet private key
- Polymarket CLOB API credentials (key, secret, passphrase)
- The wallet's USDC balance on Polygon
"""
from __future__ import annotations

from lloyd.execution.base import ExecutionResult, Executor, TradeSignal


class PolymarketLiveExecutor(Executor):
    """Stub — raises NotImplementedError until live trading is implemented."""

    async def execute(self, signal: TradeSignal) -> ExecutionResult:
        raise NotImplementedError(
            "Polymarket live execution not yet implemented. "
            "Set LLOYD_LIVE_TRADING_ENABLED=false."
        )

    async def get_current_price(
        self, market_id: int, platform: str, platform_id: str
    ) -> float | None:
        raise NotImplementedError(
            "Polymarket live price fetch not yet implemented. "
            "Set LLOYD_LIVE_TRADING_ENABLED=false."
        )

    def is_live(self) -> bool:
        return True
