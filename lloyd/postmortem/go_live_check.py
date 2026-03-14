"""Automated go/no-go evaluation against predefined criteria."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import structlog

from lloyd.config import Settings
from lloyd.postmortem.calibration import CalibrationAnalyzer, OVERALL_CATEGORY_SENTINEL
from lloyd.postmortem.metrics import MetricsCalculator

log = structlog.get_logger()


@dataclass
class CriterionResult:
    name: str
    passed: bool
    value: float | None = None
    threshold: float | None = None
    detail: str = ""


@dataclass
class GoLiveResult:
    go: bool = False
    criteria: list[CriterionResult] = field(default_factory=list)
    weakest: list[CriterionResult] = field(default_factory=list)


class GoLiveChecker:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self._conn = conn
        self._settings = settings

    def run(self) -> GoLiveResult:
        criteria = [
            self._check_brier(),
            self._check_roi(),
            self._check_calibration_error(),
            self._check_sample_size(),
            self._check_drawdown(),
            self._check_stability(),
        ]

        # Remove criteria that returned None values (insufficient data)
        valid = [c for c in criteria if c.value is not None]
        go = len(valid) == len(criteria) and all(c.passed for c in criteria)

        weakest = self._weakest_criteria(criteria)

        return GoLiveResult(go=go, criteria=criteria, weakest=weakest)

    def _check_brier(self) -> CriterionResult:
        threshold = 0.20

        latest_row = self._conn.execute(
            """SELECT period_end FROM model_scores
               WHERE category = ? AND period_type = 'alltime'
               ORDER BY period_end DESC LIMIT 1""",
            (OVERALL_CATEGORY_SENTINEL,),
        ).fetchone()

        if latest_row is None:
            return CriterionResult(
                name="brier_score", passed=False, value=None,
                threshold=threshold, detail="No Brier data available",
            )

        rows = self._conn.execute(
            """SELECT model_name, brier_score FROM model_scores
               WHERE category = ? AND period_type = 'alltime' AND period_end = ?""",
            (OVERALL_CATEGORY_SENTINEL, latest_row[0]),
        ).fetchall()

        weights = CalibrationAnalyzer(self._conn, self._settings).get_model_weights()

        if weights and len(weights) >= 2:
            weighted_sum = 0.0
            weight_total = 0.0
            for model_name, brier_score in rows:
                w = weights.get(model_name, 0.0)
                weighted_sum += brier_score * w
                weight_total += w
            brier = weighted_sum / weight_total if weight_total > 0 else rows[0][1]
        else:
            brier = sum(b for _, b in rows) / len(rows)

        return CriterionResult(
            name="brier_score", passed=brier <= threshold,
            value=brier, threshold=threshold,
        )

    def _check_roi(self) -> CriterionResult:
        threshold = 0.0
        calc = MetricsCalculator(self._conn, self._settings)
        metrics = calc.compute()

        if metrics.total_trades == 0:
            return CriterionResult(
                name="roi", passed=False, value=None,
                threshold=threshold, detail="No trades",
            )

        return CriterionResult(
            name="roi", passed=metrics.roi >= threshold,
            value=metrics.roi, threshold=threshold,
        )

    def _check_calibration_error(self) -> CriterionResult:
        """Check calibration using ensemble final_probability, not per-model."""
        threshold = 0.05

        rows = self._conn.execute(
            """SELECT ep.final_probability,
                      CASE WHEN o.outcome = 'yes' THEN 1 ELSE 0 END
               FROM ensemble_predictions ep
               JOIN outcomes o ON o.market_id = ep.market_id
               WHERE o.outcome != 'void'""",
        ).fetchall()

        if len(rows) < self._settings.min_brier_sample:
            return CriterionResult(
                name="calibration_error", passed=False, value=None,
                threshold=threshold, detail="Insufficient ensemble data",
            )

        cal_err = CalibrationAnalyzer._calibration_error(rows)

        return CriterionResult(
            name="calibration_error", passed=cal_err <= threshold,
            value=cal_err, threshold=threshold,
        )

    def _check_sample_size(self) -> CriterionResult:
        threshold = 100
        row = self._conn.execute(
            "SELECT COUNT(*) FROM trades WHERE status = 'settled'"
        ).fetchone()

        count = row[0] if row else 0
        return CriterionResult(
            name="sample_size", passed=count >= threshold,
            value=float(count), threshold=float(threshold),
        )

    def _check_drawdown(self) -> CriterionResult:
        threshold = 0.30 * self._settings.paper_bankroll
        calc = MetricsCalculator(self._conn, self._settings)
        metrics = calc.compute()

        if metrics.mc_max_drawdown_95 is None:
            return CriterionResult(
                name="max_drawdown", passed=False, value=None,
                threshold=threshold,
                detail="Insufficient trades for Monte Carlo (need 50+)",
            )

        return CriterionResult(
            name="max_drawdown",
            passed=metrics.mc_max_drawdown_95 <= threshold,
            value=metrics.mc_max_drawdown_95,
            threshold=threshold,
        )

    def _check_stability(self) -> CriterionResult:
        """Check scan cycle uptime over stability window."""
        window_days = self._settings.stability_window_days
        min_pct = self._settings.stability_min_cycle_pct
        interval = self._settings.scan_interval_minutes

        cutoff = datetime.now(timezone.utc) - timedelta(days=window_days)
        row = self._conn.execute(
            "SELECT COUNT(*) FROM scan_results WHERE scan_timestamp >= ?",
            (cutoff.isoformat(),),
        ).fetchone()

        actual_cycles = row[0] if row else 0
        expected_cycles = (window_days * 24 * 60) / interval
        pct = actual_cycles / expected_cycles if expected_cycles > 0 else 0.0

        passed = pct >= min_pct

        if passed and self._settings.log_path:
            has_errors = self._check_log_for_errors(cutoff)
            if has_errors:
                passed = False

        return CriterionResult(
            name="stability", passed=passed,
            value=pct, threshold=min_pct,
            detail=f"{actual_cycles}/{int(expected_cycles)} cycles",
        )

    def _check_log_for_errors(self, cutoff: datetime) -> bool:
        """Check if structured log contains ERROR entries within the stability window."""
        import json as _json

        log_path = Path(self._settings.log_path)
        if not log_path.exists():
            return False

        try:
            with open(log_path) as f:
                for line in f:
                    lower = line.lower()
                    if '"level":"error"' not in lower and '"level": "error"' not in lower:
                        continue
                    try:
                        entry = _json.loads(line)
                    except (ValueError, _json.JSONDecodeError):
                        continue
                    ts_str = entry.get("timestamp") or entry.get("time")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str)
                        if ts.tzinfo is None:
                            ts = ts.replace(tzinfo=timezone.utc)
                        if ts >= cutoff:
                            return True
                    except (ValueError, TypeError):
                        continue
        except Exception:
            pass

        return False

    @staticmethod
    def _weakest_criteria(criteria: list[CriterionResult]) -> list[CriterionResult]:
        """Return failing criteria sorted by how far they are from passing.

        Only includes criteria that have a measurable value (excludes
        insufficient-data results).
        """
        failing = []
        for c in criteria:
            if c.passed or c.value is None or c.threshold is None:
                continue
            gap = abs(c.value - c.threshold) / c.threshold if c.threshold != 0 else 0.0
            failing.append((gap, c))

        failing.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in failing]
