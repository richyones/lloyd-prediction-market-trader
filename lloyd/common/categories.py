"""Category normalization from Polymarket tag slugs to canonical categories.

The Gamma API ``/markets`` endpoint returns a ``tags`` array (when queried
with ``include_tag=true``) containing event-level tag objects.  Each tag has
a ``slug`` field.  This module maps those slugs to the canonical category
names used for exploitability scoring.

Mapping derived from live Gamma API data (March 2026).
"""

from __future__ import annotations

TAG_SLUG_TO_CATEGORY: dict[str, str] = {
    # Sports
    "sports": "sports",
    "soccer": "sports",
    "nba": "sports",
    "EPL": "sports",
    "serie-a": "sports",
    "nfl": "sports",
    "mlb": "sports",
    "nhl": "sports",
    "tennis": "sports",
    "golf": "sports",
    "mma": "sports",
    "f1": "sports",
    "cricket": "sports",
    # Politics
    "politics": "politics",
    "elections": "politics",
    "global-elections": "politics",
    "world-elections": "politics",
    "us-presidential-election": "politics",
    "trump": "politics",
    "trump-presidency": "politics",
    "courts": "politics",
    "congress": "politics",
    "columbia-election": "politics",
    # World events
    "world": "world_events",
    "geopolitics": "world_events",
    "ukraine": "world_events",
    "foreign-policy": "world_events",
    "world-affairs": "world_events",
    "israel": "world_events",
    # Crypto
    "crypto": "crypto",
    "airdrops": "crypto",
    # Entertainment / culture
    "pop-culture": "entertainment",
    # Finance
    "finance": "finance",
    "stocks": "finance",
    "ipos": "finance",
    "pre-market": "finance",
    "economy": "finance",
    "business": "finance",
    # Weather
    "weather": "weather",
    "climate": "weather",
}


def normalize_category(
    tags: list[dict[str, object]] | None,
    fallback: str | None = None,
) -> str | None:
    """Return the first matching canonical category for a list of tag dicts.

    Each tag dict is expected to have a ``"slug"`` key.  The first slug that
    appears in :data:`TAG_SLUG_TO_CATEGORY` wins.  Returns *fallback* when
    no tag matches (defaults to ``None``).
    """
    if not tags:
        return fallback
    for tag in tags:
        slug = tag.get("slug")
        if slug is not None and slug in TAG_SLUG_TO_CATEGORY:
            return TAG_SLUG_TO_CATEGORY[slug]
    return fallback
