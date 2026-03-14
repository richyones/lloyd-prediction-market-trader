from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from lloyd.common.models import Article, Market, NewsBundle
from lloyd.prediction.llm import (
    GPT5Predictor,
    Predictor,
    PredictionResult,
)


def _make_market(**overrides) -> Market:
    now = datetime.now(timezone.utc)
    defaults = dict(
        platform="polymarket",
        platform_id="pm-test",
        question="Will it rain tomorrow in NYC?",
        category="weather",
        current_price=0.65,
        volume=50_000,
        liquidity=5_000,
        close_date=now + timedelta(days=14),
        raw_data={},
        fetched_at=now,
    )
    defaults.update(overrides)
    return Market(**defaults)


def _make_bundle(n: int = 3) -> NewsBundle:
    articles = [
        Article(
            title=f"Article {i}",
            source=f"src-{i}",
            published_at="2026-03-10",
            snippet=f"Snippet {i}",
            url=f"https://example.com/{i}",
        )
        for i in range(n)
    ]
    quality = "good" if n >= 5 else ("partial" if n >= 1 else "none")
    return NewsBundle(articles=articles, context_quality=quality, article_count=n)


VALID_LLM_RESPONSE = json.dumps({
    "probability": 0.70,
    "confidence": 4,
    "reasoning": "Rain is very likely based on forecasts.",
    "evidence_for": "NOAA forecast says 80% chance of rain.",
    "evidence_against": "Some models show dry air moving in.",
    "market_disagree_reason": "aligned",
})


class TestParseResponse:
    def test_valid_json(self):
        result = Predictor._parse_response(VALID_LLM_RESPONSE)
        assert result["probability"] == 0.70
        assert result["confidence"] == 4

    def test_malformed_json_raises(self):
        with pytest.raises(ValueError, match="Invalid JSON"):
            Predictor._parse_response("not json at all")

    def test_missing_keys_raises(self):
        incomplete = json.dumps({"probability": 0.5, "confidence": 3})
        with pytest.raises(ValueError, match="Missing keys"):
            Predictor._parse_response(incomplete)


class TestCostCalculation:
    def test_gpt5_cost_with_actual_split(self):
        with patch("lloyd.prediction.llm.get_settings") as mock:
            mock.return_value.gpt5_model = "gpt-5"
            mock.return_value.gpt5_fallback_model = "gpt-4o"
            mock.return_value.openai_api_key = "test"
            mock.return_value.gpt5_input_cost_per_1k = 0.005
            mock.return_value.gpt5_output_cost_per_1k = 0.015
            predictor = GPT5Predictor()
            cost = predictor._calculate_cost(1500, 500)
            # (1500/1000)*0.005 + (500/1000)*0.015 = 0.0075 + 0.0075 = 0.015
            assert abs(cost - 0.015) < 1e-6


class TestRateLimiterCalledOnce:
    @pytest.mark.asyncio
    async def test_acquire_called_per_predict(self):
        """RateLimiter.acquire() is called exactly once per predict()."""
        market = _make_market()
        bundle = _make_bundle()

        class _FakePredictor(Predictor):
            async def _call_api(self, system_prompt, user_prompt):
                return VALID_LLM_RESPONSE, 80, 20

            def _model_name(self):
                return "fake-model"

            def _calculate_cost(self, input_tokens, output_tokens):
                return 0.0

        limiter = AsyncMock()
        limiter.acquire = AsyncMock()
        predictor = _FakePredictor(limiter)
        result = await predictor.predict(market, bundle)

        assert result is not None
        limiter.acquire.assert_awaited_once()


class TestGPT5Fallback:
    @pytest.mark.asyncio
    async def test_falls_back_on_not_found(self):
        """Mock AsyncOpenAI so first create() raises NotFoundError,
        second succeeds -- proving _call_api's fallback path works."""
        from openai import NotFoundError

        market = _make_market()
        bundle = _make_bundle()

        class FakeUsage:
            prompt_tokens = 100
            completion_tokens = 50

        class FakeMessage:
            content = VALID_LLM_RESPONSE

        class FakeChoice:
            message = FakeMessage()

        class FakeResponse:
            choices = [FakeChoice()]
            usage = FakeUsage()

        call_models: list[str] = []

        async def mock_create(**kwargs):
            call_models.append(kwargs.get("model", ""))
            if kwargs.get("model") == "gpt-5":
                raise NotFoundError(
                    message="Model not found",
                    response=MagicMock(status_code=404, headers={}),
                    body=None,
                )
            return FakeResponse()

        mock_client = AsyncMock()
        mock_client.chat.completions.create = mock_create

        with patch("lloyd.prediction.llm.get_settings") as mock_settings:
            mock_settings.return_value.gpt5_model = "gpt-5"
            mock_settings.return_value.gpt5_fallback_model = "gpt-4o"
            mock_settings.return_value.openai_api_key = "test"
            mock_settings.return_value.gpt5_input_cost_per_1k = 0.005
            mock_settings.return_value.gpt5_output_cost_per_1k = 0.015

            with patch("lloyd.prediction.llm.OPENAI_LIMITER") as mock_limiter:
                mock_limiter.acquire = AsyncMock()
                predictor = GPT5Predictor()

                with patch("openai.AsyncOpenAI", return_value=mock_client):
                    result = await predictor.predict(market, bundle)

        assert result is not None
        assert result.probability == 0.70
        assert call_models == ["gpt-5", "gpt-4o"]
