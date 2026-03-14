from __future__ import annotations

import asyncio
import sqlite3

import structlog
from pydantic import BaseModel

from lloyd.common.models import Market, NewsBundle, ScanResult
from lloyd.config import Settings
from lloyd.db import get_market_id, insert_ensemble_prediction, insert_predictions
from lloyd.prediction.llm import (
    ClaudeSonnetPredictor,
    GeminiPredictor,
    GPT5Predictor,
    PredictionResult,
)
from lloyd.research.cache import ResearchCache
from lloyd.research.news import NewsRetriever

log = structlog.get_logger()


class EnsemblePrediction(BaseModel):
    market_id: int
    ensemble_prediction_id: int = 0
    ensemble_probability: float
    market_price: float
    edge: float
    alpha: float
    final_probability: float
    model_predictions: list[PredictionResult]
    trade_signal: str
    tier2_used: bool


class EnsemblePipeline:
    """Orchestrates research + tiered LLM prediction for a batch of markets."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self._conn = conn
        self._settings = settings
        self._cache = ResearchCache(conn)
        self._retriever = NewsRetriever()
        self._gemini = GeminiPredictor()
        self._gpt5 = GPT5Predictor()
        self._claude = ClaudeSonnetPredictor()
        self._last_run_cost: float = 0.0

    async def run(
        self,
        candidates: list[ScanResult],
        model_weights: dict[str, float] | None = None,
    ) -> list[EnsemblePrediction]:
        results: list[EnsemblePrediction] = []
        total_cost = 0.0

        for candidate in candidates:
            market: Market = candidate.market
            market_id = get_market_id(self._conn, market)
            if market_id is None:
                log.warning("market_id_not_found", question=market.question[:80])
                continue

            query_hash = self._cache.hash_query(market.question)
            bundle = self._cache.get(market_id, query_hash)
            if bundle is None:
                bundle = await self._retriever.fetch(market)
                self._cache.set(market_id, query_hash, bundle)

            tier1 = await self._run_tier1(market, bundle)
            tier2_used = False
            tier2_result: PredictionResult | None = None

            if self._should_escalate(tier1, market.current_price):
                tier2_result = await self._run_tier2(market, bundle)
                tier2_used = True

            all_preds = [p for p in [*tier1, tier2_result] if p is not None]

            if not all_preds:
                log.warning("all_models_returned_none", question=market.question[:80])
                continue

            ep = self._aggregate(
                market_id=market_id,
                results_list=all_preds,
                market_price=market.current_price,
                tier2_used=tier2_used,
                model_weights=model_weights,
            )

            insert_predictions(self._conn, all_preds, market_id)
            ep.ensemble_prediction_id = insert_ensemble_prediction(self._conn, ep)

            total_cost += sum(p.cost_usd for p in all_preds)
            results.append(ep)

        self._last_run_cost = total_cost
        await self._retriever.close()

        buy_yes = sum(1 for e in results if e.trade_signal == "buy_yes")
        buy_no = sum(1 for e in results if e.trade_signal == "buy_no")
        no_trade = sum(1 for e in results if e.trade_signal == "no_trade")
        log.info(
            "stage_2_complete",
            predictions=len(results),
            tier2_used=sum(1 for e in results if e.tier2_used),
            total_cost_usd=round(total_cost, 4),
            buy_yes=buy_yes,
            buy_no=buy_no,
            no_trade=no_trade,
        )
        return results

    async def _run_tier1(
        self, market: Market, bundle: NewsBundle,
    ) -> list[PredictionResult | None]:
        return list(await asyncio.gather(
            self._gemini.predict(market, bundle),
            self._gpt5.predict(market, bundle),
        ))

    async def _run_tier2(
        self, market: Market, bundle: NewsBundle,
    ) -> PredictionResult | None:
        return await self._claude.predict(market, bundle)

    def _should_escalate(
        self,
        tier1_results: list[PredictionResult | None],
        market_price: float,
    ) -> bool:
        threshold = self._settings.tier1_escalation_threshold
        for r in tier1_results:
            if r is not None and abs(r.probability - market_price) > threshold:
                return True
        return False

    def _aggregate(
        self,
        market_id: int,
        results_list: list[PredictionResult],
        market_price: float,
        tier2_used: bool,
        model_weights: dict[str, float] | None = None,
    ) -> EnsemblePrediction:
        """Aggregate model probabilities with market-conditioned blending.

        When ``model_weights`` has >= 2 keys, use a pure weighted mean (no
        trimming).  Brier-derived weights already discount high-error models,
        so trimming would double-penalize and could drop valid extreme calls.

        When ``model_weights`` is None or has < 2 keys, fall back to the
        existing trimmed-mean (drop min/max when >= 3 models).
        """
        if model_weights and len(model_weights) >= 2:
            default_w = 1.0 / len(results_list)
            weighted_sum = 0.0
            weight_total = 0.0
            for r in results_list:
                w = model_weights.get(r.model_name, default_w)
                weighted_sum += r.probability * w
                weight_total += w
            ensemble_prob = weighted_sum / weight_total if weight_total > 0 else 0.5
        else:
            probabilities = [r.probability for r in results_list]
            if len(probabilities) >= 3:
                sorted_probs = sorted(probabilities)
                trimmed = sorted_probs[1:-1]
                ensemble_prob = sum(trimmed) / len(trimmed)
            else:
                ensemble_prob = sum(probabilities) / len(probabilities)

        alpha = self._settings.market_conditioned_alpha
        edge = ensemble_prob - market_price
        final_prob = (1 - alpha) * market_price + alpha * ensemble_prob

        min_edge = self._settings.min_edge_threshold
        if edge > min_edge:
            signal = "buy_yes"
        elif edge < -min_edge:
            signal = "buy_no"
        else:
            signal = "no_trade"

        return EnsemblePrediction(
            market_id=market_id,
            ensemble_probability=ensemble_prob,
            market_price=market_price,
            edge=edge,
            alpha=alpha,
            final_probability=final_prob,
            model_predictions=results_list,
            trade_signal=signal,
            tier2_used=tier2_used,
        )
