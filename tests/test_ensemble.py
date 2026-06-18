from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lloyd.common.models import Market, NewsBundle, ScanResult
from lloyd.config import Settings
from lloyd.prediction.ensemble import EnsemblePipeline, EnsemblePrediction
from lloyd.prediction.llm import PredictionResult


def _make_settings(**overrides) -> Settings:
    defaults = dict(
        min_edge_threshold=0.03,
        tier1_escalation_threshold=0.05,
        market_conditioned_alpha=0.3,
        buy_yes_alpha=0.15,
        database_path=":memory:",
    )
    defaults.update(overrides)
    return Settings(**defaults)


def _make_prediction(probability: float, model: str = "test-model") -> PredictionResult:
    return PredictionResult(
        model_name=model,
        probability=probability,
        confidence=3,
        reasoning="test reasoning",
        evidence_for="evidence for",
        evidence_against="evidence against",
        market_disagree_reason="aligned",
        tokens_used=100,
        cost_usd=0.01,
        prompt_version="v1.0",
        context_quality="good",
        input_context_hash="abc123",
    )


class TestShouldEscalate:
    def test_both_within_threshold_no_escalation(self):
        settings = _make_settings()
        conn = MagicMock()
        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = settings

        r1 = _make_prediction(0.52)
        r2 = _make_prediction(0.48)
        assert pipeline._should_escalate([r1, r2], 0.50) is False

    def test_one_diverges_triggers_escalation(self):
        settings = _make_settings()
        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = settings

        r1 = _make_prediction(0.57)
        r2 = _make_prediction(0.48)
        assert pipeline._should_escalate([r1, r2], 0.50) is True

    def test_both_none_no_escalation(self):
        settings = _make_settings()
        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = settings

        assert pipeline._should_escalate([None, None], 0.50) is False


class TestAggregate:
    def _make_pipeline(self) -> EnsemblePipeline:
        settings = _make_settings()
        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = settings
        return pipeline

    def test_trimmed_mean_three_results(self):
        pipeline = self._make_pipeline()
        results = [
            _make_prediction(0.30),
            _make_prediction(0.50),
            _make_prediction(0.70),
        ]
        ep = pipeline._aggregate(
            market_id=1, results_list=results, market_price=0.50, tier2_used=True,
        )
        assert abs(ep.ensemble_probability - 0.50) < 1e-6

    def test_plain_mean_two_results(self):
        pipeline = self._make_pipeline()
        results = [
            _make_prediction(0.40),
            _make_prediction(0.60),
        ]
        ep = pipeline._aggregate(
            market_id=1, results_list=results, market_price=0.50, tier2_used=False,
        )
        assert abs(ep.ensemble_probability - 0.50) < 1e-6

    def test_single_result(self):
        pipeline = self._make_pipeline()
        results = [_make_prediction(0.70)]
        ep = pipeline._aggregate(
            market_id=1, results_list=results, market_price=0.50, tier2_used=False,
        )
        assert abs(ep.ensemble_probability - 0.70) < 1e-6


class TestTradeSignal:
    def _make_pipeline(self, min_edge: float = 0.03) -> EnsemblePipeline:
        settings = _make_settings(min_edge_threshold=min_edge)
        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = settings
        return pipeline

    def test_buy_yes_on_positive_edge(self):
        pipeline = self._make_pipeline()
        results = [_make_prediction(0.60)]
        ep = pipeline._aggregate(
            market_id=1, results_list=results, market_price=0.50, tier2_used=False,
        )
        assert ep.edge > 0.03
        assert ep.trade_signal == "buy_yes"

    def test_buy_no_on_negative_edge(self):
        pipeline = self._make_pipeline()
        results = [_make_prediction(0.40)]
        ep = pipeline._aggregate(
            market_id=1, results_list=results, market_price=0.50, tier2_used=False,
        )
        assert ep.edge < -0.03
        assert ep.trade_signal == "buy_no"

    def test_no_trade_on_small_edge(self):
        pipeline = self._make_pipeline()
        results = [_make_prediction(0.52)]
        ep = pipeline._aggregate(
            market_id=1, results_list=results, market_price=0.50, tier2_used=False,
        )
        assert abs(ep.edge) <= 0.03
        assert ep.trade_signal == "no_trade"


class TestFinalProbability:
    def test_buy_yes_uses_buy_yes_alpha(self):
        settings = _make_settings(buy_yes_alpha=0.15, market_conditioned_alpha=0.3)
        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = settings

        results = [_make_prediction(0.80)]
        ep = pipeline._aggregate(
            market_id=1, results_list=results, market_price=0.50, tier2_used=False,
        )
        assert ep.trade_signal == "buy_yes"
        assert ep.alpha == 0.15
        # final = 0.85 * 0.50 + 0.15 * 0.80 = 0.545
        assert abs(ep.final_probability - 0.545) < 1e-6

    def test_buy_no_uses_market_conditioned_alpha(self):
        settings = _make_settings(buy_yes_alpha=0.15, market_conditioned_alpha=0.3)
        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = settings

        results = [_make_prediction(0.20)]
        ep = pipeline._aggregate(
            market_id=1, results_list=results, market_price=0.50, tier2_used=False,
        )
        assert ep.trade_signal == "buy_no"
        assert ep.alpha == 0.3
        # final = 0.7 * 0.50 + 0.3 * 0.20 = 0.41
        assert abs(ep.final_probability - 0.41) < 1e-6

    def test_market_conditioned_blend(self):
        settings = _make_settings(market_conditioned_alpha=0.3, buy_yes_alpha=0.15)
        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = settings

        results = [_make_prediction(0.80)]
        ep = pipeline._aggregate(
            market_id=1, results_list=results, market_price=0.50, tier2_used=False,
        )
        # buy_yes signal uses buy_yes_alpha: 0.85 * 0.50 + 0.15 * 0.80 = 0.545
        assert abs(ep.final_probability - 0.545) < 1e-6
        assert ep.alpha == 0.15


class TestRunTier1:
    @pytest.mark.asyncio
    async def test_both_tier1_models_called(self):
        now = datetime.now(timezone.utc)
        market = Market(
            platform="polymarket",
            platform_id="pm-1",
            question="Test?",
            category="crypto",
            current_price=0.50,
            volume=50_000,
            close_date=now + timedelta(days=30),
            raw_data={},
            fetched_at=now,
        )
        bundle = NewsBundle(articles=[], context_quality="none", article_count=0)

        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = _make_settings()
        pipeline._gpt5 = AsyncMock()
        pipeline._gemini = AsyncMock()

        gpt_pred = _make_prediction(0.55, model="gpt-5")
        gemini_pred = _make_prediction(0.52, model="gemini-2.5-flash")
        pipeline._gpt5.predict = AsyncMock(return_value=gpt_pred)
        pipeline._gemini.predict = AsyncMock(return_value=gemini_pred)

        results = await pipeline._run_tier1(market, bundle)

        pipeline._gpt5.predict.assert_awaited_once_with(market, bundle)
        pipeline._gemini.predict.assert_awaited_once_with(market, bundle)
        assert results == [gpt_pred, gemini_pred]

    @pytest.mark.asyncio
    async def test_gemini_none_continues_with_gpt_only(self):
        now = datetime.now(timezone.utc)
        market = Market(
            platform="polymarket",
            platform_id="pm-1",
            question="Test?",
            category="crypto",
            current_price=0.50,
            volume=50_000,
            close_date=now + timedelta(days=30),
            raw_data={},
            fetched_at=now,
        )
        bundle = NewsBundle(articles=[], context_quality="none", article_count=0)

        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._settings = _make_settings()
        pipeline._gpt5 = AsyncMock()
        pipeline._gemini = AsyncMock()

        gpt_pred = _make_prediction(0.55, model="gpt-5")
        pipeline._gpt5.predict = AsyncMock(return_value=gpt_pred)
        pipeline._gemini.predict = AsyncMock(return_value=None)

        results = await pipeline._run_tier1(market, bundle)

        assert results == [gpt_pred, None]


class TestAllModelsNone:
    @pytest.mark.asyncio
    async def test_market_skipped_when_all_none(self):
        now = datetime.now(timezone.utc)
        market = Market(
            platform="polymarket",
            platform_id="pm-1",
            question="Test?",
            category="crypto",
            current_price=0.50,
            volume=50_000,
            close_date=now + timedelta(days=30),
            raw_data={},
            fetched_at=now,
        )
        candidate = ScanResult(
            market=market,
            exploitability_score=1.0,
            passed_filter=True,
            scan_timestamp=now,
        )

        settings = _make_settings()
        conn = MagicMock()
        pipeline = EnsemblePipeline.__new__(EnsemblePipeline)
        pipeline._conn = conn
        pipeline._settings = settings
        pipeline._cache = MagicMock()
        pipeline._cache.hash_query.return_value = "abc"
        pipeline._cache.get.return_value = NewsBundle(
            articles=[], context_quality="none", article_count=0,
        )
        pipeline._retriever = AsyncMock()
        pipeline._retriever.close = AsyncMock()
        pipeline._gemini = AsyncMock()
        pipeline._gpt5 = AsyncMock()
        pipeline._claude = AsyncMock()
        pipeline._last_run_cost = 0.0

        pipeline._gemini.predict = AsyncMock(return_value=None)
        pipeline._gpt5.predict = AsyncMock(return_value=None)

        with patch("lloyd.prediction.ensemble.get_market_id", return_value=1):
            with patch("lloyd.prediction.ensemble.insert_predictions"):
                with patch("lloyd.prediction.ensemble.insert_ensemble_prediction", return_value=1):
                    results = await pipeline.run([candidate])

        assert len(results) == 0
