from __future__ import annotations

from datetime import datetime, timezone

from lloyd.common.models import Market, NewsBundle

PROMPT_VERSION = "v1.1"

CATEGORY_GUIDANCE: dict[str, str] = {
    "weather": (
        "Weather forecasts and meteorological data are highly reliable "
        "— weight official forecasts heavily."
    ),
    "entertainment": (
        "Award outcomes and entertainment events are driven by industry "
        "sentiment and campaign spending — look for insider signals."
    ),
    "politics": (
        "Political prediction markets tend to underreact to polling "
        "movements — check if recent polls are already priced in."
    ),
    "sports": (
        "Sports outcomes depend heavily on current form, injuries, and "
        "head-to-head records — prioritize recent data."
    ),
    "default": (
        "Consider base rates for similar historical events and any recent "
        "developments the market may not have priced in."
    ),
}

_SYSTEM_PROMPT = """\
You are a calibrated probability forecaster. Your task is to estimate the
true probability of a binary event resolving YES, given available evidence.
You are known for being precise, well-reasoned, and honest about uncertainty.
Do not hedge. Do not give ranges. Give a single probability."""

_USER_TEMPLATE = """\
EVENT: {market_question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {current_price:.2f} (reflects crowd consensus, which is informative but not always accurate)
TIME TO RESOLUTION: {days_remaining}
CATEGORY: {category}
CATEGORY GUIDANCE: {category_guidance}

RECENT NEWS AND CONTEXT:
{news_context}

INSTRUCTIONS:
1. Consider evidence for and against the event resolving YES.
2. Identify information the market may not have priced in.
3. The market price is a useful reference — crowds are often right, but systematically misprice niche, low-liquidity, and fast-moving events. Deviate when your evidence supports it.
4. Be precise. Give a single float probability between 0.01 and 0.99.

Respond in JSON only. No preamble. No explanation outside the JSON.
{{
  "probability": <float 0.01 to 0.99>,
  "confidence": <int 1-5, where 5 is very confident>,
  "reasoning": "<2-3 sentences: your key factors and how they moved you from the market price>",
  "evidence_for": "<strongest evidence that the event resolves YES>",
  "evidence_against": "<strongest evidence that the event resolves NO>",
  "market_disagree_reason": "<explain your estimate relative to the market price and the key reason you converged or diverged>"
}}"""


def format_news_context(bundle: NewsBundle) -> str:
    if bundle.context_quality == "none" or not bundle.articles:
        return (
            "No recent news articles were found for this market. "
            "Base your estimate on priors and the market price alone."
        )
    lines: list[str] = []
    for i, a in enumerate(bundle.articles, 1):
        lines.append(
            f"{i}. [{a.source}] {a.title} ({a.published_at})\n"
            f"   {a.snippet}\n"
            f"   {a.url}"
        )
    return "\n".join(lines)


def build_prompt(
    market: Market,
    bundle: NewsBundle,
    category_guidance: str,
) -> tuple[str, str]:
    """Return ``(system_prompt, user_prompt)`` for the LLM call."""
    resolution_criteria = (
        market.raw_data.get("description")
        or market.raw_data.get("resolutionCriteria")
        or "Per market rules."
    )

    if market.close_date is not None:
        close_aware = (
            market.close_date
            if market.close_date.tzinfo is not None
            else market.close_date.replace(tzinfo=timezone.utc)
        )
        days = (close_aware - datetime.now(timezone.utc)).days
        days_remaining = f"{days} days"
    else:
        days_remaining = "Unknown"

    news_context = format_news_context(bundle)

    user_prompt = _USER_TEMPLATE.format(
        market_question=market.question,
        resolution_criteria=resolution_criteria,
        current_price=market.current_price,
        days_remaining=days_remaining,
        category=market.category or "general",
        category_guidance=category_guidance,
        news_context=news_context,
    )
    return _SYSTEM_PROMPT, user_prompt
