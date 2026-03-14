from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone

from lloyd.common.models import Article, NewsBundle
from lloyd.config import get_settings


class ResearchCache:
    """Read/write wrapper around the ``research_cache`` table (2 h TTL)."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._ttl_hours = get_settings().news_cache_ttl_hours

    def hash_query(self, question: str) -> str:
        """SHA-256 hex digest of the lowercased, stripped question."""
        return hashlib.sha256(question.lower().strip().encode()).hexdigest()

    def get(self, market_id: int, query_hash: str) -> NewsBundle | None:
        row = self._conn.execute(
            "SELECT articles, context_quality, article_count, expires_at "
            "FROM research_cache WHERE market_id = ? AND query_hash = ?",
            (market_id, query_hash),
        ).fetchone()
        if row is None:
            return None

        articles_json, quality, count, expires_at_str = row
        expires_at = datetime.fromisoformat(expires_at_str)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        if expires_at <= datetime.now(timezone.utc):
            self._conn.execute(
                "DELETE FROM research_cache WHERE market_id = ? AND query_hash = ?",
                (market_id, query_hash),
            )
            self._conn.commit()
            return None

        articles = [Article(**a) for a in json.loads(articles_json)]
        return NewsBundle(
            articles=articles,
            context_quality=quality,
            article_count=count,
        )

    def set(self, market_id: int, query_hash: str, bundle: NewsBundle) -> None:
        now = datetime.now(timezone.utc)
        expires_at = now + timedelta(hours=self._ttl_hours)

        articles_json = json.dumps(
            [
                {
                    "title": a.title,
                    "source": a.source,
                    "published_at": a.published_at,
                    "snippet": a.snippet,
                    "url": a.url,
                    "sentiment_score": a.sentiment_score,
                }
                for a in bundle.articles
            ]
        )

        self._conn.execute(
            """INSERT OR REPLACE INTO research_cache
               (market_id, query_hash, articles, context_quality,
                article_count, fetched_at, expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                market_id,
                query_hash,
                articles_json,
                bundle.context_quality,
                bundle.article_count,
                now.isoformat(),
                expires_at.isoformat(),
            ),
        )
        self._conn.commit()
