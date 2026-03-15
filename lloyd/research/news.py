from __future__ import annotations

import asyncio
import re
import string

import httpx
import structlog

from lloyd.common.models import Article, Market, NewsBundle
from lloyd.config import get_settings

log = structlog.get_logger()

STOPWORDS: set[str] = {
    "the", "a", "an", "is", "are", "will", "be", "to", "of",
    "in", "on", "at", "by", "for", "and", "or", "not", "it",
    "this", "that", "with", "from", "has", "have", "was", "were",
    "do", "does", "did", "can", "could", "would", "should",
}

GDELT_URL = (
    "https://api.gdeltproject.org/api/v2/doc/doc"
    "?query={query}&mode=artlist&maxrecords=25&timespan=7d&format=json"
)


class NewsRetriever:
    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0)

    async def close(self) -> None:
        await self._client.aclose()

    async def fetch(self, market: Market) -> NewsBundle:
        keywords = self._extract_keywords(market.question)
        gdelt_articles, rss_articles = await asyncio.gather(
            self._fetch_gdelt(" ".join(keywords)),
            self._fetch_rss(keywords),
            return_exceptions=True,
        )
        if isinstance(gdelt_articles, BaseException):
            log.error("gdelt_unexpected_error", error=str(gdelt_articles))
            gdelt_articles = []
        if isinstance(rss_articles, BaseException):
            log.error("rss_unexpected_error", error=str(rss_articles))
            rss_articles = []

        merged = list(gdelt_articles) + list(rss_articles)
        merged = self._deduplicate(merged)
        merged = merged[:25]

        quality = self._score_quality(merged)
        return NewsBundle(
            articles=merged,
            context_quality=quality,
            article_count=len(merged),
        )

    async def _fetch_gdelt(self, query: str) -> list[Article]:
        if not hasattr(self, "_gdelt_sem"):
            self._gdelt_sem = asyncio.Semaphore(5)
        try:
            async with self._gdelt_sem:
                resp = await self._client.get(
                    GDELT_URL.format(query=query),
                    timeout=5.0,
                )
            if resp.status_code == 429:
                log.warning("gdelt_fetch_error", error="429 Too Many Requests")
                return []
            resp.raise_for_status()
            data = resp.json()
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            log.warning("gdelt_fetch_error", error=str(exc))
            return []
        except Exception as exc:
            log.warning("gdelt_parse_error", error=str(exc))
            return []

        raw_articles = data.get("articles", [])
        if not isinstance(raw_articles, list):
            log.warning("gdelt_unexpected_shape", keys=list(data.keys()))
            return []

        articles: list[Article] = []
        for item in raw_articles:
            try:
                tone_raw = item.get("tone", "")
                if tone_raw and isinstance(tone_raw, str):
                    sentiment = float(tone_raw.split(",")[0])
                elif isinstance(tone_raw, (int, float)):
                    sentiment = float(tone_raw)
                else:
                    sentiment = None

                articles.append(Article(
                    title=item.get("title", ""),
                    source=item.get("domain", item.get("source", "")),
                    published_at=item.get("seendate", ""),
                    snippet=item.get("title", ""),
                    url=item.get("url", ""),
                    sentiment_score=sentiment,
                ))
            except (KeyError, ValueError, TypeError):
                continue
        return articles

    async def _fetch_rss(self, keywords: list[str]) -> list[Article]:
        import feedparser

        settings = get_settings()
        feed_urls = settings.rss_feeds
        if not feed_urls:
            return []

        async def _parse_one(url: str) -> list[Article]:
            try:
                feed = await asyncio.to_thread(feedparser.parse, url)
            except Exception as exc:
                log.warning("rss_parse_error", url=url, error=str(exc))
                return []
            results: list[Article] = []
            for entry in feed.entries:
                title = getattr(entry, "title", "") or ""
                description = getattr(entry, "description", "") or ""
                text = f"{title} {description}".lower()
                matches = sum(1 for kw in keywords if kw in text)
                if matches < 2:
                    continue
                published = getattr(entry, "published", "") or ""
                link = getattr(entry, "link", "") or ""
                results.append(Article(
                    title=title,
                    source=url,
                    published_at=published,
                    snippet=description[:300],
                    url=link,
                    sentiment_score=None,
                ))
            return results

        per_feed = await asyncio.gather(
            *[_parse_one(url) for url in feed_urls],
            return_exceptions=True,
        )
        all_articles: list[Article] = []
        for batch in per_feed:
            if isinstance(batch, BaseException):
                continue
            all_articles.extend(batch)

        all_articles.sort(key=lambda a: a.published_at, reverse=True)
        return all_articles[:25]

    def _extract_keywords(self, question: str) -> list[str]:
        cleaned = question.translate(str.maketrans("", "", string.punctuation))
        tokens = cleaned.lower().split()
        return [t for t in tokens if t not in STOPWORDS]

    def _deduplicate(self, articles: list[Article]) -> list[Article]:
        from rapidfuzz.fuzz import token_sort_ratio

        kept: list[Article] = []
        for article in articles:
            is_dup = False
            for existing in kept:
                if token_sort_ratio(article.title, existing.title) > 85:
                    is_dup = True
                    break
            if not is_dup:
                kept.append(article)
        return kept

    @staticmethod
    def _score_quality(articles: list[Article]) -> str:
        n = len(articles)
        if n >= 5:
            return "good"
        if n >= 1:
            return "partial"
        return "none"
