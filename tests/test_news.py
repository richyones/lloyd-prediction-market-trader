from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from lloyd.common.models import Article, NewsBundle
from lloyd.research.news import NewsRetriever


@pytest.fixture()
def retriever():
    return NewsRetriever()


GDELT_RESPONSE = {
    "articles": [
        {
            "url": "https://reuters.com/article1",
            "title": "NYC weather forecast: rain expected",
            "seendate": "20260310T120000Z",
            "domain": "reuters.com",
            "sourcecountry": "US",
            "tone": "1.5,-0.3,1.8,0.5,20.1,5.2,300",
        },
        {
            "url": "https://bbc.co.uk/article2",
            "title": "Rain likely in northeastern US this week",
            "seendate": "20260310T100000Z",
            "domain": "bbc.co.uk",
            "sourcecountry": "GB",
            "tone": "-0.2,0.1,-0.3,0.2,18.0,4.0,250",
        },
        {
            "url": "https://nytimes.com/article3",
            "title": "Weather patterns shift over NYC area",
            "seendate": "20260309T080000Z",
            "domain": "nytimes.com",
            "sourcecountry": "US",
            "tone": "0.0",
        },
    ]
}


class TestExtractKeywords:
    def test_strips_punctuation_and_stopwords(self, retriever):
        keywords = retriever._extract_keywords("Will it rain in NYC tomorrow?")
        assert "will" not in keywords
        assert "it" not in keywords
        assert "in" not in keywords
        assert "rain" in keywords
        assert "nyc" in keywords
        assert "tomorrow" in keywords

    def test_returns_list(self, retriever):
        result = retriever._extract_keywords("Simple question?")
        assert isinstance(result, list)

    def test_empty_input(self, retriever):
        assert retriever._extract_keywords("") == []

    def test_all_stopwords(self, retriever):
        assert retriever._extract_keywords("will it be to the") == []


class TestGDELTFetching:
    @pytest.mark.asyncio
    async def test_parses_valid_response(self, retriever):
        mock_resp = httpx.Response(
            200,
            json=GDELT_RESPONSE,
            request=httpx.Request("GET", "https://api.gdeltproject.org/"),
        )
        retriever._client = AsyncMock()
        retriever._client.get = AsyncMock(return_value=mock_resp)

        articles = await retriever._fetch_gdelt("rain NYC")
        assert len(articles) == 3
        assert articles[0].source == "reuters.com"
        assert articles[0].sentiment_score == 1.5

    @pytest.mark.asyncio
    async def test_http_500_returns_empty(self, retriever):
        mock_resp = httpx.Response(
            500,
            request=httpx.Request("GET", "https://api.gdeltproject.org/"),
        )
        retriever._client = AsyncMock()
        retriever._client.get = AsyncMock(side_effect=httpx.HTTPStatusError(
            "Server Error", request=mock_resp.request, response=mock_resp,
        ))

        articles = await retriever._fetch_gdelt("rain NYC")
        assert articles == []

    @pytest.mark.asyncio
    async def test_malformed_json_returns_empty(self, retriever):
        mock_resp = httpx.Response(
            200,
            content=b"not json",
            request=httpx.Request("GET", "https://api.gdeltproject.org/"),
        )
        retriever._client = AsyncMock()
        retriever._client.get = AsyncMock(return_value=mock_resp)

        articles = await retriever._fetch_gdelt("rain NYC")
        assert articles == []


class TestRSSRelevanceFilter:
    @pytest.mark.asyncio
    async def test_two_keyword_threshold(self, retriever):
        class FakeEntry:
            title = "NYC rain forecast is gloomy"
            description = "Heavy rain expected in NYC this week"
            published = "Mon, 10 Mar 2026 12:00:00 GMT"
            link = "https://example.com/rain"

        class FakeEntryOneKeyword:
            title = "General weather update"
            description = "Conditions are changing globally"
            published = "Mon, 10 Mar 2026 12:00:00 GMT"
            link = "https://example.com/weather"

        class FakeFeed:
            entries = [FakeEntry(), FakeEntryOneKeyword()]

        with patch("lloyd.research.news.get_settings") as mock_settings:
            mock_settings.return_value.rss_feeds = ["https://example.com/rss"]
            with patch(
                "lloyd.research.news.asyncio.to_thread",
                new_callable=AsyncMock,
                return_value=FakeFeed(),
            ):
                articles = await retriever._fetch_rss(["rain", "nyc", "forecast"])

        assert len(articles) == 1
        assert "rain" in articles[0].title.lower()


class TestDeduplication:
    def test_near_duplicate_removed(self, retriever):
        articles = [
            Article(
                title="NYC rain forecast for this week",
                source="reuters.com",
                published_at="2026-03-10",
                snippet="Snippet 1",
                url="https://reuters.com/1",
            ),
            Article(
                title="NYC rain forecast for this week — update",
                source="bbc.co.uk",
                published_at="2026-03-10",
                snippet="Snippet 2",
                url="https://bbc.co.uk/2",
            ),
        ]
        result = retriever._deduplicate(articles)
        assert len(result) == 1
        assert result[0].source == "reuters.com"

    def test_distinct_articles_kept(self, retriever):
        articles = [
            Article(
                title="NYC weather looking wet",
                source="reuters.com",
                published_at="2026-03-10",
                snippet="Snippet 1",
                url="https://reuters.com/1",
            ),
            Article(
                title="Global markets rally on tech earnings",
                source="bbc.co.uk",
                published_at="2026-03-10",
                snippet="Snippet 2",
                url="https://bbc.co.uk/2",
            ),
        ]
        result = retriever._deduplicate(articles)
        assert len(result) == 2


class TestContextQuality:
    def test_zero_articles_none(self, retriever):
        assert retriever._score_quality([]) == "none"

    def test_three_articles_partial(self, retriever):
        arts = [Article(title="t", source="s", published_at="d", snippet="s", url="u")] * 3
        assert retriever._score_quality(arts) == "partial"

    def test_seven_articles_good(self, retriever):
        arts = [Article(title="t", source="s", published_at="d", snippet="s", url="u")] * 7
        assert retriever._score_quality(arts) == "good"

    def test_five_articles_good(self, retriever):
        arts = [Article(title="t", source="s", published_at="d", snippet="s", url="u")] * 5
        assert retriever._score_quality(arts) == "good"

    def test_one_article_partial(self, retriever):
        arts = [Article(title="t", source="s", published_at="d", snippet="s", url="u")]
        assert retriever._score_quality(arts) == "partial"
