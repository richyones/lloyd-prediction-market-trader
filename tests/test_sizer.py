"""Tests for lloyd.risk.sizer — quarter-Kelly sizing + hard risk limits."""
from __future__ import annotations

import pytest

from lloyd.common.models import PortfolioState
from lloyd.config import Settings
from lloyd.prediction.llm import PredictionResult
from lloyd.prediction.ensemble import EnsemblePrediction
from lloyd.risk.sizer import RiskSizer


def _pred(
    probability: float = 0.60,
    confidence: int = 4,
) -> PredictionResult:
    """Shorthand for a synthetic model prediction."""
    return PredictionResult(
        model_name="test-model",
        probability=probability,
        confidence=confidence,
        reasoning="test reasoning text " * 5,
        evidence_for="evidence for",
        evidence_against="evidence against",
        market_disagree_reason="none",
        tokens_used=100,
        cost_usd=0.001,
        prompt_version="v1",
        context_quality="good",
        input_context_hash="abc123",
    )


def _ensemble(
    *,
    edge: float = 0.08,
    final_probability: float = 0.58,
    market_price: float = 0.50,
    trade_signal: str = "buy_yes",
    model_predictions: list[PredictionResult] | None = None,
    ensemble_prediction_id: int = 1,
) -> EnsemblePrediction:
    preds = model_predictions or [_pred(), _pred()]
    return EnsemblePrediction(
        market_id=1,
        ensemble_prediction_id=ensemble_prediction_id,
        ensemble_probability=0.58,
        market_price=market_price,
        edge=edge,
        alpha=0.3,
        final_probability=final_probability,
        model_predictions=preds,
        trade_signal=trade_signal,
        tier2_used=False,
    )


def _portfolio(
    cash: float = 10_000.0,
    exposure: float = 0.0,
    positions: list[dict] | None = None,
) -> PortfolioState:
    return PortfolioState(
        cash_balance=cash,
        total_exposure=exposure,
        positions=positions or [],
    )


def _settings(**overrides) -> Settings:
    defaults = dict(
        database_path=":memory:",
        min_edge_threshold=0.03,
        kelly_fraction=0.25,
        max_position_pct=0.05,
        max_exposure_pct=0.20,
        min_confidence=3.0,
    )
    defaults.update(overrides)
    return Settings(**defaults)


_BASE_KWARGS = dict(platform="polymarket", platform_id="cond-1", category="crypto")


class TestEdgeCheck:
    def test_edge_below_threshold_blocks(self):
        sizer = RiskSizer(_settings())
        ep = _ensemble(edge=0.02, trade_signal="buy_yes")
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is None

    def test_edge_at_threshold_passes(self):
        sizer = RiskSizer(_settings())
        ep = _ensemble(edge=0.03, trade_signal="buy_yes")
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is not None


class TestConfidenceCheck:
    def test_low_confidence_blocks(self):
        sizer = RiskSizer(_settings())
        low_conf = [_pred(confidence=2), _pred(confidence=2)]
        ep = _ensemble(model_predictions=low_conf)
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is None

    def test_high_confidence_passes(self):
        sizer = RiskSizer(_settings())
        high_conf = [_pred(confidence=4), _pred(confidence=5)]
        ep = _ensemble(model_predictions=high_conf)
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is not None


class TestCategoryConcentration:
    def test_three_positions_blocks(self):
        sizer = RiskSizer(_settings())
        positions = [
            {"trade_id": i, "market_id": i, "platform": "polymarket",
             "direction": "buy_yes", "quantity": 10, "entry_price": 0.50,
             "category": "crypto"}
            for i in range(3)
        ]
        portfolio = _portfolio(cash=9_000, exposure=1_500, positions=positions)
        ep = _ensemble()
        result = sizer.size(ep, portfolio, **_BASE_KWARGS)
        assert result is None

    def test_two_positions_passes(self):
        sizer = RiskSizer(_settings())
        positions = [
            {"trade_id": i, "market_id": i, "platform": "polymarket",
             "direction": "buy_yes", "quantity": 10, "entry_price": 0.50,
             "category": "crypto"}
            for i in range(2)
        ]
        portfolio = _portfolio(cash=9_000, exposure=1_000, positions=positions)
        ep = _ensemble()
        result = sizer.size(ep, portfolio, **_BASE_KWARGS)
        assert result is not None


class TestExposureCap:
    def test_at_exposure_limit_blocks(self):
        sizer = RiskSizer(_settings(max_exposure_pct=0.20))
        portfolio = _portfolio(cash=8_000, exposure=2_000)
        ep = _ensemble()
        result = sizer.size(ep, portfolio, **_BASE_KWARGS)
        assert result is None

    def test_just_under_exposure_limit_passes(self):
        sizer = RiskSizer(_settings(max_exposure_pct=0.20))
        portfolio = _portfolio(cash=9_500, exposure=100)
        ep = _ensemble()
        result = sizer.size(ep, portfolio, **_BASE_KWARGS)
        assert result is not None


class TestDisagreementKill:
    def test_buy_yes_with_low_prob_model_blocks(self):
        """One model at P=0.15 when signal is buy_yes => disagree => block."""
        sizer = RiskSizer(_settings())
        preds = [_pred(probability=0.65), _pred(probability=0.15)]
        ep = _ensemble(trade_signal="buy_yes", model_predictions=preds)
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is None

    def test_buy_no_with_high_prob_model_blocks(self):
        """One model at P=0.85 when signal is buy_no => disagree => block."""
        sizer = RiskSizer(_settings())
        preds = [_pred(probability=0.40), _pred(probability=0.85)]
        ep = _ensemble(
            trade_signal="buy_no",
            edge=-0.08,
            final_probability=0.42,
            market_price=0.50,
            model_predictions=preds,
        )
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is None


class TestKellyMath:
    def test_buy_yes_known_values(self):
        """
        edge = 0.08, final_probability = 0.58, price = 0.58
        odds = (1/0.58) - 1 = 0.7241
        fraction = 0.25 * 0.08 / 0.7241 = 0.02762
        dollar_size = 0.02762 * 10000 = 276.23
        quantity = 276.23 / 0.58 = 476.25
        """
        sizer = RiskSizer(_settings())
        ep = _ensemble(edge=0.08, final_probability=0.58)
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is not None
        assert result.direction == "buy_yes"
        assert result.limit_price == pytest.approx(0.58)

        odds = (1 / 0.58) - 1
        expected_fraction = 0.25 * 0.08 / odds
        expected_dollar = expected_fraction * 10_000
        expected_qty = expected_dollar / 0.58
        assert result.quantity == pytest.approx(expected_qty, rel=1e-4)

    def test_buy_no_known_values(self):
        """
        trade_signal=buy_no, final_probability=0.42, market_price=0.50
        edge on EnsemblePrediction = -0.08
        In sizer: price = 1 - 0.42 = 0.58, edge = 0.08
        odds = (1/0.58) - 1 = 0.7241
        fraction = 0.25 * 0.08 / 0.7241 = 0.02762
        dollar_size = 0.02762 * 10000 = 276.23
        quantity = 276.23 / 0.58 = 476.25
        """
        sizer = RiskSizer(_settings())
        ep = _ensemble(
            trade_signal="buy_no",
            edge=-0.08,
            final_probability=0.42,
            market_price=0.50,
        )
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is not None
        assert result.direction == "buy_no"

        price = 1 - 0.42
        odds = (1 / price) - 1
        expected_fraction = 0.25 * 0.08 / odds
        expected_dollar = expected_fraction * 10_000
        expected_qty = expected_dollar / price
        assert result.quantity == pytest.approx(expected_qty, rel=1e-4)


    def test_buy_no_limit_price_is_no_price(self):
        """limit_price for buy_no should be 1 - final_probability, not final_probability."""
        sizer = RiskSizer(_settings())
        ep = _ensemble(
            trade_signal="buy_no",
            edge=-0.08,
            final_probability=0.42,
            market_price=0.50,
        )
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is not None
        assert result.limit_price == pytest.approx(1 - 0.42, rel=1e-6)


class TestPositionSizeClamp:
    def test_kelly_exceeds_cap_is_clamped(self):
        """Kelly recommends ~2.76% but if we lower max_position_pct to 0.02
        (2%), the signal should still be returned with clamped quantity."""
        sizer = RiskSizer(_settings(max_position_pct=0.02))
        ep = _ensemble(edge=0.08, final_probability=0.58)
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is not None
        max_dollar = 0.02 * 10_000
        expected_qty = max_dollar / 0.58
        assert result.quantity == pytest.approx(expected_qty, rel=1e-4)


class TestCleanTrade:
    def test_all_limits_pass(self):
        sizer = RiskSizer(_settings())
        ep = _ensemble()
        result = sizer.size(ep, _portfolio(), **_BASE_KWARGS)
        assert result is not None
        assert result.direction == "buy_yes"
        assert result.platform == "polymarket"
        assert result.platform_id == "cond-1"
        assert result.market_id == 1
        assert result.ensemble_prediction_id == 1
        assert result.quantity > 0
