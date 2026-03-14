"""Tests for lloyd.execution.paper — paper trading executor."""
from __future__ import annotations

import asyncio
import json

import pytest

from lloyd.config import Settings
from lloyd.db import get_connection, init_db, insert_markets, insert_trade
from lloyd.common.models import Market
from lloyd.execution.base import TradeSignal
from lloyd.execution.paper import PaperExecutor

from datetime import datetime, timedelta, timezone


def _settings(**overrides) -> Settings:
    defaults = dict(
        database_path=":memory:",
        paper_bankroll=10_000.0,
        paper_slippage_pct=0.005,
        polymarket_fee_rate=0.001,
        kalshi_fee_rate=0.07,
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_db_with_market(platform="polymarket", platform_id="cond-1", category="crypto"):
    """Create an in-memory DB with one market row and return (conn, market_id)."""
    conn = get_connection(":memory:")
    init_db(conn)
    now = datetime.now(timezone.utc)
    m = Market(
        platform=platform,
        platform_id=platform_id,
        question="Test market?",
        category=category,
        current_price=0.50,
        volume=50_000,
        liquidity=5_000,
        close_date=now + timedelta(days=30),
        raw_data={},
        fetched_at=now,
    )
    insert_markets(conn, [m])
    row = conn.execute(
        "SELECT id FROM markets WHERE platform_id = ?", (platform_id,)
    ).fetchone()
    # Also insert a dummy ensemble_prediction for FK
    conn.execute(
        """INSERT INTO ensemble_predictions
           (market_id, ensemble_probability, market_price, edge, alpha,
            final_probability, model_predictions, trade_signal, tier2_used, created_at)
           VALUES (?, 0.58, 0.50, 0.08, 0.3, 0.58, '[]', 'buy_yes', 0, ?)""",
        (row[0], now.isoformat()),
    )
    conn.commit()
    ep_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    return conn, row[0], ep_id


def _signal(
    market_id=1, ensemble_prediction_id=1, platform="polymarket",
    platform_id="cond-1", direction="buy_yes", quantity=100.0,
    limit_price=0.50,
) -> TradeSignal:
    return TradeSignal(
        market_id=market_id,
        ensemble_prediction_id=ensemble_prediction_id,
        platform=platform,
        platform_id=platform_id,
        direction=direction,
        quantity=quantity,
        limit_price=limit_price,
        category="crypto",
    )


class TestExecute:
    def test_slippage_buy_yes(self):
        """executed_price = limit_price * (1 + 0.005) for buy_yes."""
        conn, mid, ep_id = _make_db_with_market()
        settings = _settings()
        executor = PaperExecutor(conn, settings)
        sig = _signal(market_id=mid, ensemble_prediction_id=ep_id, limit_price=0.50)
        result = asyncio.get_event_loop().run_until_complete(executor.execute(sig))

        assert result.executed_price == pytest.approx(0.50 * 1.005, rel=1e-6)
        assert result.slippage == pytest.approx(0.50 * 0.005, rel=1e-6)
        assert result.is_paper is True
        assert result.status == "open"
        conn.close()

    def test_slippage_buy_no(self):
        """executed_price = limit_price * (1 - 0.005) for buy_no."""
        conn, mid, ep_id = _make_db_with_market()
        settings = _settings()
        executor = PaperExecutor(conn, settings)
        sig = _signal(
            market_id=mid, ensemble_prediction_id=ep_id,
            direction="buy_no", limit_price=0.50,
        )
        result = asyncio.get_event_loop().run_until_complete(executor.execute(sig))

        assert result.executed_price == pytest.approx(0.50 * 0.995, rel=1e-6)
        assert result.slippage == pytest.approx(-0.50 * 0.005, rel=1e-6)
        conn.close()


class TestFeeCalculation:
    def test_polymarket_fee(self):
        """fee = 0.001 * quantity * executed_price."""
        conn, mid, ep_id = _make_db_with_market()
        settings = _settings()
        executor = PaperExecutor(conn, settings)
        sig = _signal(
            market_id=mid, ensemble_prediction_id=ep_id,
            platform="polymarket", quantity=100.0, limit_price=0.50,
        )
        result = asyncio.get_event_loop().run_until_complete(executor.execute(sig))

        expected_price = 0.50 * 1.005
        expected_fee = 0.001 * 100.0 * expected_price
        assert result.fee == pytest.approx(expected_fee, rel=1e-4)
        conn.close()

    def test_kalshi_fee_at_030(self):
        """fee = 0.07 * quantity * min(0.30, 0.70) = 0.07 * qty * 0.30."""
        conn, mid, ep_id = _make_db_with_market(platform="kalshi", platform_id="TICK-1")
        settings = _settings()
        executor = PaperExecutor(conn, settings)
        sig = _signal(
            market_id=mid, ensemble_prediction_id=ep_id,
            platform="kalshi", platform_id="TICK-1",
            quantity=100.0, limit_price=0.30,
        )
        result = asyncio.get_event_loop().run_until_complete(executor.execute(sig))

        exec_price = 0.30 * 1.005
        expected_fee = 0.07 * 100.0 * min(exec_price, 1 - exec_price)
        assert result.fee == pytest.approx(expected_fee, rel=1e-4)
        conn.close()

    def test_kalshi_fee_at_070_symmetric(self):
        """fee at p=0.70 should equal fee at p=0.30 (symmetry of min(p, 1-p))."""
        conn, mid, ep_id = _make_db_with_market(platform="kalshi", platform_id="TICK-2")
        settings = _settings()
        executor = PaperExecutor(conn, settings)

        sig_low = _signal(
            market_id=mid, ensemble_prediction_id=ep_id,
            platform="kalshi", platform_id="TICK-2",
            quantity=100.0, limit_price=0.30,
        )
        sig_high = _signal(
            market_id=mid, ensemble_prediction_id=ep_id,
            platform="kalshi", platform_id="TICK-2",
            quantity=100.0, limit_price=0.70,
        )
        fee_low = executor._calculate_fee("kalshi", 100.0, 0.30)
        fee_high = executor._calculate_fee("kalshi", 100.0, 0.70)
        assert fee_low == pytest.approx(fee_high, rel=1e-6)
        conn.close()

    def test_kalshi_fee_at_050_maximum(self):
        """fee = 0.07 * quantity * 0.50 (maximum fee at midpoint)."""
        conn, mid, ep_id = _make_db_with_market(platform="kalshi", platform_id="TICK-3")
        settings = _settings()
        executor = PaperExecutor(conn, settings)
        expected = 0.07 * 100.0 * 0.50
        actual = executor._calculate_fee("kalshi", 100.0, 0.50)
        assert actual == pytest.approx(expected, rel=1e-6)
        conn.close()


class TestPortfolioState:
    def test_aggregates_multiple_open_positions(self):
        conn, mid, ep_id = _make_db_with_market()
        settings = _settings(paper_bankroll=10_000.0)
        executor = PaperExecutor(conn, settings)

        insert_trade(
            conn, market_id=mid, ensemble_prediction_id=ep_id,
            platform="polymarket", direction="buy_yes",
            quantity=50.0, limit_price=0.50, executed_price=0.5025,
            slippage=0.0025, fee=0.025, is_paper=True,
            status="open", opened_at="2026-01-01T00:00:00Z",
        )
        insert_trade(
            conn, market_id=mid, ensemble_prediction_id=ep_id,
            platform="polymarket", direction="buy_yes",
            quantity=30.0, limit_price=0.60, executed_price=0.603,
            slippage=0.003, fee=0.018, is_paper=True,
            status="open", opened_at="2026-01-01T01:00:00Z",
        )

        state = executor.get_portfolio_state()
        expected_exposure = 50.0 * 0.5025 + 30.0 * 0.603
        assert state.total_exposure == pytest.approx(expected_exposure, rel=1e-4)
        assert state.cash_balance == pytest.approx(10_000.0 - expected_exposure, rel=1e-4)
        assert len(state.positions) == 2
        conn.close()


class TestSnapshotPortfolio:
    def test_writes_row_with_correct_fields(self):
        conn, mid, ep_id = _make_db_with_market()
        settings = _settings(paper_bankroll=10_000.0)
        executor = PaperExecutor(conn, settings)

        insert_trade(
            conn, market_id=mid, ensemble_prediction_id=ep_id,
            platform="polymarket", direction="buy_yes",
            quantity=100.0, limit_price=0.50, executed_price=0.5025,
            slippage=0.0025, fee=0.05, is_paper=True,
            status="open", opened_at="2026-01-01T00:00:00Z",
        )

        executor.snapshot_portfolio()

        row = conn.execute("SELECT * FROM portfolio ORDER BY id DESC LIMIT 1").fetchone()
        assert row is not None
        # cash_balance
        expected_exposure = 100.0 * 0.5025
        assert row[2] == pytest.approx(10_000.0 - expected_exposure, rel=1e-4)
        # total_exposure
        assert row[3] == pytest.approx(expected_exposure, rel=1e-4)
        # unrealized_pnl should be NULL (no prices passed)
        assert row[4] is None
        # num_open_positions
        assert row[6] == 1
        # snapshot should be valid JSON
        snapshot = json.loads(row[7])
        assert len(snapshot) == 1
        conn.close()
