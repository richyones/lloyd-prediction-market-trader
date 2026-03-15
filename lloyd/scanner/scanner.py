from __future__ import annotations

import math
from datetime import datetime, timezone

import structlog

from lloyd.common.models import Market, ScanResult
from lloyd.config import Settings

log = structlog.get_logger()

CATEGORY_MULTIPLIERS: dict[str, float] = {
    "entertainment": 1.5,
    "weather": 1.5,
    "world_events": 1.3,
    "sports": 1.0,
    "crypto": 1.0,
    "politics": 0.8,
    "finance": 0.5,
}
DEFAULT_MULTIPLIER = 1.0


class MarketScanner:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def scan(self, markets: list[Market]) -> list[ScanResult]:
        now = datetime.now(timezone.utc)
        log.info("scan_start", total_markets=len(markets))

        filtered = self._filter_volume(markets)
        filtered = self._filter_time(filtered)
        filtered = self._filter_price(filtered)
        filtered = self._filter_liquidity(filtered)

        results = [
            ScanResult(
                market=m,
                exploitability_score=self._score(m),
                passed_filter=True,
                scan_timestamp=now,
            )
            for m in filtered
        ]
        results.sort(key=lambda r: r.exploitability_score, reverse=True)

        log.info("scan_complete", candidates=len(results))
        return results

    def _filter_volume(self, markets: list[Market]) -> list[Market]:
        out = [m for m in markets if m.volume > self._settings.min_volume]
        log.info("filter_volume", before=len(markets), after=len(out))
        return out

    def _filter_time(self, markets: list[Market]) -> list[Market]:
        now = datetime.now(timezone.utc)
        out: list[Market] = []
        for m in markets:
            if m.close_date is None:
                continue
            close_aware = (
                m.close_date
                if m.close_date.tzinfo is not None
                else m.close_date.replace(tzinfo=timezone.utc)
            )
            days = (close_aware - now).total_seconds() / 86400
            if self._settings.min_days_to_resolution <= days <= self._settings.max_days_to_resolution:
                out.append(m)
        log.info("filter_time", before=len(markets), after=len(out))
        return out

    def _filter_price(self, markets: list[Market]) -> list[Market]:
        out = [m for m in markets if 0.05 < m.current_price < 0.95]
        log.info("filter_price", before=len(markets), after=len(out))
        return out

    def _filter_liquidity(self, markets: list[Market]) -> list[Market]:
        out: list[Market] = []
        for m in markets:
            if m.liquidity is not None:
                # Polymarket: use actual liquidity field
                if m.liquidity > self._settings.min_liquidity:
                    out.append(m)
            else:
                # Kalshi: liquidity field is deprecated (always None)
                # Require open_interest > 500 contracts as a proxy
                if m.open_interest is not None and m.open_interest > 500:
                    out.append(m)
        log.info("filter_liquidity", before=len(markets), after=len(out))
        return out

    def _score(self, market: Market) -> float:
        if market.category is not None:
            cat_mult = CATEGORY_MULTIPLIERS.get(market.category, DEFAULT_MULTIPLIER)
        else:
            cat_mult = DEFAULT_MULTIPLIER

        volume_score = math.log10(max(market.volume, 1)) / 7.0
        extreme_price_bonus = abs(0.5 - market.current_price) * 0.5
        return cat_mult * (volume_score + extreme_price_bonus)
