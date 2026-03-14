from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class TradeSignal(BaseModel):
    """Signal produced by the risk sizer for the executor to fill."""

    market_id: int
    ensemble_prediction_id: int
    platform: str
    platform_id: str
    direction: str
    quantity: float
    limit_price: float
    category: str | None = None


class ExecutionResult(BaseModel):
    """Result returned after an order is placed (paper or live)."""

    trade_id: int
    market_id: int
    platform: str
    direction: str
    quantity: float
    limit_price: float
    executed_price: float
    slippage: float
    fee: float
    is_paper: bool
    status: str
    opened_at: str


class Executor(ABC):
    """Abstract executor interface implemented by paper and live executors."""

    @abstractmethod
    async def execute(self, signal: TradeSignal) -> ExecutionResult: ...

    @abstractmethod
    async def get_current_price(
        self, market_id: int, platform: str, platform_id: str
    ) -> float | None: ...

    @abstractmethod
    def is_live(self) -> bool: ...
