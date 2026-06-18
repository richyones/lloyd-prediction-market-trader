from __future__ import annotations

from datetime import datetime, timezone

from lloyd.common.models import Market, NewsBundle

PROMPT_VERSION = "v1.2"

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
You are a calibrated probability forecaster specialising in prediction markets.
Your task is to estimate the true probability of a binary event resolving YES.

Critical principles:
- Prediction market prices reflect the aggregate view of informed, financially-motivated
  traders. The market price is your prior. To justify deviating from it, you need a
  specific, articulable information edge — not just a plausible narrative.
- You are susceptible to narrative bias: vivid news coverage makes events feel more
  probable than base rates warrant. An event being actively discussed does not mean
  it is likely. Counteract this deliberately.
- For low-probability markets (price below 0.20), the burden of proof for a buy_yes
  recommendation is high. Most such markets resolve NO. You need strong, specific
  evidence — not general plausibility — to justify a higher probability.
- Do not hedge. Do not give ranges. Give a single probability."""

_USER_TEMPLATE = """\
EVENT: {market_question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {current_price:.2f}
TIME TO RESOLUTION: {days_remaining}
CATEGORY: {category}
CATEGORY GUIDANCE: {category_guidance}

RECENT NEWS AND CONTEXT:
{news_context}

INSTRUCTIONS — work through these steps before giving your final answer:

STEP 1 — BASE RATE
What percentage of similar events historically resolve YES? Consider: how often do \
events of this type (geopolitical actions, price thresholds, specific outcomes) actually \
occur within comparable timeframes? State your base rate estimate in one sentence.

STEP 2 — INFORMATION EDGE
What specific information do you have that the market has not yet fully priced in? \
If you cannot identify a concrete information edge, the market price is likely correct. \
A compelling narrative is not an edge.

STEP 3 — STEELMAN THE NO CASE
What are the two or three strongest reasons this event resolves NO? Force yourself to \
engage with these seriously before finalising your probability.

STEP 4 — FINAL PROBABILITY
Given the base rate, your information edge (or lack of one), and the NO case, what is \
your probability? If you have no clear edge over the market, stay within 0.03 of the \
market price.

Respond in JSON only. No preamble. No explanation outside the JSON.
{{
  "probability": <float 0.01 to 0.99>,
  "confidence": <int 1-5, where 5 is very confident>,
  "reasoning": "<2-3 sentences: base rate, your key adjustment, and final position>",
  "evidence_for": "<strongest concrete evidence that the event resolves YES>",
  "evidence_against": "<strongest concrete evidence that the event resolves NO>",
  "market_disagree_reason": "<state your edge over the market, or confirm you have none>"
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
