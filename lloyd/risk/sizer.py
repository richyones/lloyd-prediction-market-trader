from __future__ import annotations

import structlog

from lloyd.common.models import PortfolioState
from lloyd.config import Settings, get_settings
from lloyd.execution.base import TradeSignal
from lloyd.prediction.ensemble import EnsemblePrediction

log = structlog.get_logger()


class RiskSizer:
    """Quarter-Kelly position sizing with hard risk limits.

    Pure logic — no async, no DB writes, no API calls.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self._s = settings or get_settings()

    def size(
        self,
        prediction: EnsemblePrediction,
        portfolio: PortfolioState,
        *,
        platform: str,
        platform_id: str,
        category: str | None = None,
    ) -> TradeSignal | None:
        """Return a ``TradeSignal`` or ``None`` if the trade is blocked."""
        direction = prediction.trade_signal
        if direction == "no_trade":
            return None

        # --- Edge check ---
        if abs(prediction.edge) < self._s.min_edge_threshold:
            log.info("trade_blocked", market_id=prediction.market_id, reason="edge_below_threshold")
            return None

        # --- Confidence check ---
        confidences = [
            p.confidence for p in prediction.model_predictions if p is not None
        ]
        if not confidences:
            log.info("trade_blocked", market_id=prediction.market_id, reason="no_model_predictions")
            return None
        mean_confidence = sum(confidences) / len(confidences)
        if mean_confidence < self._s.min_confidence:
            log.info("trade_blocked", market_id=prediction.market_id, reason="confidence_below_threshold")
            return None

        # --- Disagreement kill ---
        if self._disagreement_kill(prediction):
            log.info("trade_blocked", market_id=prediction.market_id, reason="model_disagreement")
            return None

        # --- Category concentration ---
        if category is not None and portfolio.exposure_by_category(category) >= 3:
            log.info("trade_blocked", market_id=prediction.market_id, reason="category_concentration")
            return None

        # --- Kelly sizing ---
        if direction == "buy_yes":
            price = prediction.final_probability
            edge = prediction.edge
        else:
            price = 1 - prediction.final_probability
            edge = -prediction.edge

        fraction = self._kelly_fraction(edge, price)
        dollar_size = fraction * portfolio.cash_balance
        if dollar_size <= 0:
            log.info("trade_blocked", market_id=prediction.market_id, reason="non_positive_kelly")
            return None

        # --- Position size cap (clamp, not block) ---
        max_dollar = self._s.max_position_pct * portfolio.cash_balance
        if dollar_size > max_dollar:
            log.info(
                "position_clamped",
                market_id=prediction.market_id,
                kelly_pct=round(fraction, 4),
                clamped_to=round(max_dollar, 2),
            )
            dollar_size = max_dollar

        # --- Exposure cap ---
        total_portfolio = portfolio.cash_balance + portfolio.total_exposure
        if total_portfolio > 0 and (
            portfolio.total_exposure + dollar_size
            > self._s.max_exposure_pct * total_portfolio
        ):
            log.info("trade_blocked", market_id=prediction.market_id, reason="exposure_cap")
            return None

        quantity = dollar_size / price if price > 0 else 0.0
        if quantity <= 0:
            log.info("trade_blocked", market_id=prediction.market_id, reason="zero_quantity")
            return None

        return TradeSignal(
            market_id=prediction.market_id,
            ensemble_prediction_id=prediction.ensemble_prediction_id,
            platform=platform,
            platform_id=platform_id,
            direction=direction,
            quantity=quantity,
            limit_price=prediction.final_probability if direction == "buy_yes" else 1 - prediction.final_probability,
            category=category,
        )

    def _kelly_fraction(self, edge: float, price: float) -> float:
        """Quarter-Kelly: fraction = KELLY_FRACTION * edge / odds.

        For buy_yes:
          odds = (1 / price) - 1  (dollars won per dollar bet if YES resolves)
        For buy_no the caller already flips price to (1 - final_probability),
        so the same formula applies symmetrically.
        """
        if price <= 0 or price >= 1:
            return 0.0
        odds = (1.0 / price) - 1.0
        if odds <= 0:
            return 0.0
        return self._s.kelly_fraction * edge / odds

    def _disagreement_kill(self, prediction: EnsemblePrediction) -> bool:
        """Block if any model strongly disagrees with the trade direction.

        For buy_yes: block if any model assigns probability < 0.20
        For buy_no:  block if any model assigns probability > 0.80
        """
        for p in prediction.model_predictions:
            if p is None:
                continue
            if prediction.trade_signal == "buy_yes" and p.probability < 0.20:
                return True
            if prediction.trade_signal == "buy_no" and p.probability > 0.80:
                return True
        return False
