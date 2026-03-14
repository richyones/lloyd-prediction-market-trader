"""Tests for executor interface compliance."""
from __future__ import annotations

import asyncio

import pytest

from lloyd.execution.base import Executor, TradeSignal
from lloyd.execution.kalshi_live import KalshiLiveExecutor
from lloyd.execution.paper import PaperExecutor
from lloyd.execution.polymarket_live import PolymarketLiveExecutor


class TestSubclassCompliance:
    def test_paper_executor_is_executor(self):
        assert issubclass(PaperExecutor, Executor)

    def test_polymarket_live_is_executor(self):
        assert issubclass(PolymarketLiveExecutor, Executor)

    def test_kalshi_live_is_executor(self):
        assert issubclass(KalshiLiveExecutor, Executor)


class TestIsLive:
    def test_paper_is_not_live(self):
        from lloyd.db import get_connection, init_db

        conn = get_connection(":memory:")
        init_db(conn)
        executor = PaperExecutor(conn)
        assert executor.is_live() is False
        conn.close()

    def test_polymarket_live_is_live(self):
        executor = PolymarketLiveExecutor()
        assert executor.is_live() is True

    def test_kalshi_live_is_live(self):
        executor = KalshiLiveExecutor()
        assert executor.is_live() is True


class TestLiveStubsRaise:
    def test_polymarket_execute_raises(self):
        executor = PolymarketLiveExecutor()
        signal = TradeSignal(
            market_id=1, ensemble_prediction_id=1,
            platform="polymarket", platform_id="cond-1",
            direction="buy_yes", quantity=10.0, limit_price=0.50,
        )
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(executor.execute(signal))

    def test_polymarket_get_current_price_raises(self):
        executor = PolymarketLiveExecutor()
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(
                executor.get_current_price(1, "polymarket", "cond-1")
            )

    def test_kalshi_execute_raises(self):
        executor = KalshiLiveExecutor()
        signal = TradeSignal(
            market_id=1, ensemble_prediction_id=1,
            platform="kalshi", platform_id="TICK-1",
            direction="buy_yes", quantity=10.0, limit_price=0.50,
        )
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(executor.execute(signal))

    def test_kalshi_get_current_price_raises(self):
        executor = KalshiLiveExecutor()
        with pytest.raises(NotImplementedError):
            asyncio.get_event_loop().run_until_complete(
                executor.get_current_price(1, "kalshi", "TICK-1")
            )
