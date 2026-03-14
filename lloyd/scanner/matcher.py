from __future__ import annotations

from datetime import datetime, timezone

import structlog
from rapidfuzz.fuzz import token_sort_ratio

from lloyd.common.models import Market, MarketPair

log = structlog.get_logger()


class MarketMatcher:
    SIMILARITY_THRESHOLD: float = 80.0
    MIN_QUESTION_LENGTH: int = 10

    def match(
        self,
        polymarket_markets: list[Market],
        kalshi_markets: list[Market],
    ) -> list[MarketPair]:
        now = datetime.now(timezone.utc)

        poly = [m for m in polymarket_markets if len(m.question) >= self.MIN_QUESTION_LENGTH]
        kalshi = [m for m in kalshi_markets if len(m.question) >= self.MIN_QUESTION_LENGTH]

        pairs: list[MarketPair] = []
        for pm in poly:
            for km in kalshi:
                score = token_sort_ratio(pm.question, km.question)
                if score > self.SIMILARITY_THRESHOLD:
                    pairs.append(
                        MarketPair(
                            polymarket_market=pm,
                            kalshi_market=km,
                            similarity_score=score,
                            price_divergence=abs(pm.current_price - km.current_price),
                            matched_at=now,
                        )
                    )

        pairs.sort(key=lambda p: p.price_divergence, reverse=True)

        log.info(
            "matcher_complete",
            polymarket_candidates=len(poly),
            kalshi_candidates=len(kalshi),
            pairs_found=len(pairs),
        )
        return pairs
