from __future__ import annotations

from datetime import datetime, timedelta, timezone

from lloyd.common.models import Article, Market, NewsBundle
from lloyd.prediction.prompts.templates import (
    CATEGORY_GUIDANCE,
    PROMPT_VERSION,
    build_prompt,
    format_news_context,
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


def _make_bundle(n_articles: int = 3) -> NewsBundle:
    articles = [
        Article(
            title=f"Article {i}",
            source=f"source-{i}.com",
            published_at="2026-03-10",
            snippet=f"Snippet for article {i}.",
            url=f"https://example.com/{i}",
            sentiment_score=0.5,
        )
        for i in range(n_articles)
    ]
    if n_articles >= 5:
        quality = "good"
    elif n_articles >= 1:
        quality = "partial"
    else:
        quality = "none"
    return NewsBundle(articles=articles, context_quality=quality, article_count=n_articles)


class TestBuildPrompt:
    def test_contains_market_question(self):
        market = _make_market()
        bundle = _make_bundle()
        guidance = CATEGORY_GUIDANCE["weather"]
        _, user = build_prompt(market, bundle, guidance)
        assert market.question in user

    def test_contains_current_price(self):
        market = _make_market(current_price=0.42)
        bundle = _make_bundle()
        guidance = CATEGORY_GUIDANCE["default"]
        _, user = build_prompt(market, bundle, guidance)
        assert "0.42" in user

    def test_contains_category_guidance(self):
        market = _make_market(category="sports")
        bundle = _make_bundle()
        guidance = CATEGORY_GUIDANCE["sports"]
        _, user = build_prompt(market, bundle, guidance)
        assert "injuries" in user.lower()

    def test_days_remaining_unknown_when_no_close_date(self):
        market = _make_market(close_date=None)
        bundle = _make_bundle()
        _, user = build_prompt(market, bundle, CATEGORY_GUIDANCE["default"])
        assert "Unknown" in user

    def test_resolution_criteria_from_raw_data(self):
        market = _make_market(
            raw_data={"description": "Rain gauge must read >0.01 inches."},
        )
        bundle = _make_bundle()
        _, user = build_prompt(market, bundle, CATEGORY_GUIDANCE["default"])
        assert "Rain gauge" in user

    def test_resolution_criteria_fallback(self):
        market = _make_market(raw_data={})
        bundle = _make_bundle()
        _, user = build_prompt(market, bundle, CATEGORY_GUIDANCE["default"])
        assert "Per market rules." in user


class TestFormatNewsContext:
    def test_non_empty_bundle(self):
        bundle = _make_bundle(3)
        text = format_news_context(bundle)
        assert "1." in text
        assert "Article 0" in text
        assert "source-0.com" in text

    def test_empty_bundle_fallback(self):
        bundle = NewsBundle(articles=[], context_quality="none", article_count=0)
        text = format_news_context(bundle)
        assert "No recent news articles" in text

    def test_none_quality_triggers_fallback(self):
        bundle = NewsBundle(articles=[], context_quality="none", article_count=0)
        text = format_news_context(bundle)
        assert "priors" in text.lower()


class TestPromptVersion:
    def test_version_string(self):
        assert PROMPT_VERSION == "v1.0"
