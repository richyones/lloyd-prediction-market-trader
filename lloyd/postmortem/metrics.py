"""Portfolio-level performance metrics (read-only, no DB writes)."""
from __future__ import annotations

import math
import random
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime

import structlog

from lloyd.config import Settings

log = structlog.get_logger()


@dataclass
class PerformanceMetrics:
    win_rate: float = 0.0
    roi: float = 0.0
    pseudo_sharpe: float = 0.0
    max_drawdown: float = 0.0
    mc_max_drawdown_95: float | None = None
    kelly_adherence: float = 0.0
    total_trades: int = 0
    total_pnl: float = 0.0
    category_breakdown: dict[str, dict[str, float]] = field(default_factory=dict)


class MetricsCalculator:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self._conn = conn
        self._settings = settings

    def compute(self) -> PerformanceMetrics:
        trades = self._load_settled_trades()
        if not trades:
            return PerformanceMetrics()

        pnls = [t["pnl"] for t in trades]

        return PerformanceMetrics(
            win_rate=self._win_rate(pnls),
            roi=self._roi(pnls),
            pseudo_sharpe=self._pseudo_sharpe(trades),
            max_drawdown=self._max_drawdown(pnls),
            mc_max_drawdown_95=self._monte_carlo_max_drawdown(pnls),
            kelly_adherence=self._kelly_adherence(trades),
            total_trades=len(trades),
            total_pnl=sum(pnls),
            category_breakdown=self._category_breakdown(trades),
        )

    def _load_settled_trades(self) -> list[dict]:
        rows = self._conn.execute(
            """SELECT t.id, t.direction, t.quantity, t.executed_price, t.fee,
                      t.pnl, t.opened_at, t.closed_at, m.category,
                      ep.final_probability, ep.edge
               FROM trades t
               JOIN markets m ON m.id = t.market_id
               JOIN ensemble_predictions ep ON ep.id = t.ensemble_prediction_id
               WHERE t.status = 'settled' AND t.pnl IS NOT NULL
               ORDER BY t.closed_at""",
        ).fetchall()

        return [
            {
                "id": r[0],
                "direction": r[1],
                "quantity": r[2],
                "executed_price": r[3],
                "fee": r[4],
                "pnl": r[5],
                "opened_at": r[6],
                "closed_at": r[7],
                "category": r[8],
                "final_probability": r[9],
                "edge": r[10],
            }
            for r in rows
        ]

    @staticmethod
    def _win_rate(pnls: list[float]) -> float:
        if not pnls:
            return 0.0
        return sum(1 for p in pnls if p > 0) / len(pnls)

    def _roi(self, pnls: list[float]) -> float:
        if not pnls:
            return 0.0
        return sum(pnls) / self._settings.paper_bankroll

    @staticmethod
    def _pseudo_sharpe(trades: list[dict]) -> float:
        """Annualized Sharpe using actual observed period."""
        pnls = [t["pnl"] for t in trades]
        if len(pnls) < 2:
            return 0.0

        dates = []
        for t in trades:
            if t["closed_at"]:
                dates.append(datetime.fromisoformat(t["closed_at"]))

        if len(dates) < 2:
            return 0.0

        earliest = min(dates)
        latest = max(dates)
        elapsed_seconds = (latest - earliest).total_seconds()
        elapsed_years = elapsed_seconds / (365.25 * 24 * 3600)

        if elapsed_years <= 0:
            return 0.0

        trades_per_year = len(pnls) / elapsed_years

        mean_pnl = sum(pnls) / len(pnls)
        variance = sum((p - mean_pnl) ** 2 for p in pnls) / (len(pnls) - 1)
        std_pnl = math.sqrt(variance) if variance > 0 else 0.0

        if std_pnl <= 0:
            return 0.0

        return (mean_pnl / std_pnl) * math.sqrt(trades_per_year)

    @staticmethod
    def _max_drawdown(pnls: list[float]) -> float:
        if not pnls:
            return 0.0

        cumulative = 0.0
        peak = 0.0
        max_dd = 0.0

        for p in pnls:
            cumulative += p
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd

        return max_dd

    def _monte_carlo_max_drawdown(self, pnls: list[float]) -> float | None:
        """95th percentile max drawdown via Monte Carlo.

        Returns None if fewer than 50 trades (insufficient for meaningful simulation).
        """
        if len(pnls) < 50:
            return None

        n_sims = self._settings.mc_simulations
        drawdowns = []

        for _ in range(n_sims):
            shuffled = random.sample(pnls, len(pnls))
            drawdowns.append(self._max_drawdown(shuffled))

        drawdowns.sort()
        idx = int(n_sims * 0.95)
        return drawdowns[min(idx, len(drawdowns) - 1)]

    def _kelly_adherence(self, trades: list[dict]) -> float:
        """Mean |actual_fraction - theoretical_kelly_fraction| across trades."""
        if not trades:
            return 0.0

        deviations = []
        bankroll = self._settings.paper_bankroll

        for t in trades:
            edge = t["edge"]
            final_prob = t["final_probability"]
            direction = t["direction"]
            quantity = t["quantity"]
            executed_price = t["executed_price"]

            if direction == "buy_yes":
                price = final_prob
            else:
                price = 1 - final_prob
                edge = -edge

            if price <= 0 or price >= 1:
                continue

            odds = (1.0 / price) - 1.0
            if odds <= 0:
                continue

            theoretical = self._settings.kelly_fraction * edge / odds
            actual = (quantity * executed_price) / bankroll if bankroll > 0 else 0

            deviations.append(abs(actual - theoretical))

        if not deviations:
            return 0.0

        return 1.0 - min(sum(deviations) / len(deviations), 1.0)

    @staticmethod
    def _category_breakdown(trades: list[dict]) -> dict[str, dict[str, float]]:
        by_cat: dict[str, list[float]] = {}
        for t in trades:
            cat = t["category"] or "uncategorized"
            by_cat.setdefault(cat, []).append(t["pnl"])

        result = {}
        for cat, pnls in by_cat.items():
            result[cat] = {
                "total_pnl": sum(pnls),
                "trades": len(pnls),
                "win_rate": sum(1 for p in pnls if p > 0) / len(pnls) if pnls else 0.0,
            }
        return result
