"""Tests for lloyd.main — stage 3 execution dedup."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lloyd.config import Settings
from lloyd.db import Trade
from lloyd.execution.base import ExecutionResult, TradeSignal
from lloyd.main import _run_stage3
from lloyd.prediction.ensemble import EnsemblePrediction
from lloyd.prediction.llm import PredictionResult


def _prediction(market_id: int, trade_signal: str = "buy_yes") -> EnsemblePrediction:
    return EnsemblePrediction(
        market_id=market_id,
        ensemble_prediction_id=1,
        ensemble_probability=0.58,
        market_price=0.50,
        edge=0.08,
        alpha=0.3,
        final_probability=0.58,
        model_predictions=[
            PredictionResult(
                model_name="test",
                probability=0.58,
                confidence=4,
                reasoning="test",
                evidence_for="",
                evidence_against="",
                market_disagree_reason="",
                tokens_used=10,
                cost_usd=0.001,
                prompt_version="v1",
                context_quality="good",
                input_context_hash="abc",
            )
        ],
        trade_signal=trade_signal,
        tier2_used=False,
    )


def _open_trade(market_id: int, direction: str) -> Trade:
    return Trade(
        id=1,
        market_id=market_id,
        platform="polymarket",
        platform_id=f"cond-{market_id}",
        direction=direction,
        quantity=100.0,
        executed_price=0.50,
    )


def _mock_executor() -> MagicMock:
    mock_executor = MagicMock()
    mock_executor.get_portfolio_state.return_value = MagicMock()
    mock_executor.execute = AsyncMock()
    return mock_executor


@pytest.mark.asyncio
async def test_stage3_same_direction_blocked_silently():
    conn = MagicMock()
    settings = Settings(database_path=":memory:", live_trading_enabled=False)
    predictions = [_prediction(market_id=1, trade_signal="buy_no")]
    mock_executor = _mock_executor()
    mock_log = MagicMock()

    with (
        patch("lloyd.main.get_open_paper_trades", return_value=[_open_trade(1, "buy_no")]),
        patch("lloyd.execution.paper.PaperExecutor", return_value=mock_executor),
        patch("lloyd.risk.sizer.RiskSizer", return_value=MagicMock()),
        patch("lloyd.main.log", mock_log),
    ):
        await _run_stage3(conn, settings, predictions, MagicMock(), MagicMock())

    mock_executor.execute.assert_not_called()
    contrary_warnings = [
        call
        for call in mock_log.warning.call_args_list
        if call.args and call.args[0] == "contrary_position_blocked"
    ]
    assert contrary_warnings == []
    mock_log.info.assert_any_call(
        "trade_blocked",
        market_id=1,
        reason="existing_open_position",
    )


@pytest.mark.asyncio
async def test_stage3_contrary_direction_blocked_loudly():
    conn = MagicMock()
    settings = Settings(database_path=":memory:", live_trading_enabled=False)
    predictions = [_prediction(market_id=1, trade_signal="buy_yes")]
    mock_executor = _mock_executor()
    mock_log = MagicMock()

    with (
        patch("lloyd.main.get_open_paper_trades", return_value=[_open_trade(1, "buy_no")]),
        patch("lloyd.execution.paper.PaperExecutor", return_value=mock_executor),
        patch("lloyd.risk.sizer.RiskSizer", return_value=MagicMock()),
        patch("lloyd.main.log", mock_log),
    ):
        await _run_stage3(conn, settings, predictions, MagicMock(), MagicMock())

    mock_executor.execute.assert_not_called()
    mock_log.warning.assert_called_once_with(
        "contrary_position_blocked",
        market_id=1,
        existing_direction="buy_no",
        new_signal="buy_yes",
        edge=0.08,
    )


@pytest.mark.asyncio
async def test_stage3_skips_markets_with_existing_open_position():
    conn = MagicMock()
    settings = Settings(database_path=":memory:", live_trading_enabled=False)
    predictions = [
        _prediction(market_id=42, trade_signal="buy_yes"),
        _prediction(market_id=99, trade_signal="buy_yes"),
    ]
    poly_client = MagicMock()
    kalshi_client = MagicMock()

    open_trade = Trade(
        id=1,
        market_id=42,
        platform="polymarket",
        platform_id="cond-42",
        direction="buy_yes",
        quantity=100.0,
        executed_price=0.50,
    )

    trade_signal = TradeSignal(
        market_id=99,
        ensemble_prediction_id=1,
        platform="polymarket",
        platform_id="cond-99",
        direction="buy_yes",
        quantity=100.0,
        limit_price=0.50,
        category="crypto",
    )

    mock_executor = MagicMock()
    mock_executor.get_portfolio_state.return_value = MagicMock()
    mock_executor.execute = AsyncMock(
        return_value=ExecutionResult(
            trade_id=2,
            market_id=99,
            platform="polymarket",
            direction="buy_yes",
            quantity=100.0,
            limit_price=0.50,
            executed_price=0.50,
            slippage=0.001,
            fee=0.01,
            is_paper=True,
            status="open",
            opened_at="2026-01-01T00:00:00Z",
        )
    )

    mock_sizer = MagicMock()
    mock_sizer.size.return_value = trade_signal

    with (
        patch("lloyd.main.get_open_paper_trades", return_value=[open_trade]),
        patch("lloyd.main.get_market_info", return_value=("polymarket", "cond-99", "crypto")),
        patch("lloyd.execution.paper.PaperExecutor", return_value=mock_executor),
        patch("lloyd.risk.sizer.RiskSizer", return_value=mock_sizer),
    ):
        await _run_stage3(conn, settings, predictions, poly_client, kalshi_client)

    mock_sizer.size.assert_called_once()
    assert mock_sizer.size.call_args[0][0].market_id == 99
    mock_executor.execute.assert_called_once_with(trade_signal)
