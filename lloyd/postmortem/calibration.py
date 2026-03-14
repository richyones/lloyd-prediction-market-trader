"""Brier scoring, calibration metrics, and model-weight derivation."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import structlog

from lloyd.config import Settings

log = structlog.get_logger()

OVERALL_CATEGORY_SENTINEL = ""


class CalibrationAnalyzer:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self._conn = conn
        self._settings = settings

    def run(self) -> None:
        """Compute all-time and rolling-30d Brier/calibration for every model."""
        models = self._get_model_names()
        now = datetime.now(timezone.utc)
        earliest = self._earliest_prediction_date()
        if earliest is None:
            log.info("calibration_no_data")
            return

        for model_name in models:
            for category in self._get_categories(model_name) + [OVERALL_CATEGORY_SENTINEL]:
                cat_filter = category if category != OVERALL_CATEGORY_SENTINEL else None

                # All-time
                preds = self._load_resolved_predictions(model_name, cat_filter)
                if len(preds) >= self._settings.min_brier_sample:
                    brier = self._brier_score(preds)
                    cal_err = self._calibration_error(preds)
                    self._write_scores(
                        model_name, category, "alltime", brier, cal_err,
                        len(preds), earliest, now,
                    )

                # Rolling 30-day
                cutoff = now - timedelta(days=30)
                rolling = self._load_resolved_predictions(model_name, cat_filter, since=cutoff)
                if len(rolling) >= self._settings.min_brier_sample:
                    brier_r = self._brier_score(rolling)
                    cal_r = self._calibration_error(rolling)
                    self._write_scores(
                        model_name, category, "rolling", brier_r, cal_r,
                        len(rolling), cutoff, now,
                    )

        log.info("calibration_complete", models=len(models))

    def get_model_weights(self) -> dict[str, float] | None:
        """Return normalized inverse-Brier weights from the latest all-time scores."""
        rows = self._conn.execute(
            """SELECT model_name, brier_score, period_end
               FROM model_scores
               WHERE category = ? AND period_type = 'alltime'
               ORDER BY period_end DESC""",
            (OVERALL_CATEGORY_SENTINEL,),
        ).fetchall()

        if not rows:
            return None

        scores: dict[str, float] = {}
        latest_end = rows[0][2]
        for model_name, brier, period_end in rows:
            if period_end == latest_end:
                scores[model_name] = brier

        if len(scores) < 2:
            return None

        inv = {m: 1.0 / b if b > 0 else 1.0 for m, b in scores.items()}
        total = sum(inv.values())
        if total <= 0:
            return None

        return {m: w / total for m, w in inv.items()}

    def flag_category_leaders(self) -> dict[str, str]:
        """Return {category: best_model_name} based on latest all-time Brier."""
        rows = self._conn.execute(
            """SELECT category, model_name, brier_score
               FROM model_scores
               WHERE category != ? AND period_type = 'alltime'
               ORDER BY period_end DESC, brier_score ASC""",
            (OVERALL_CATEGORY_SENTINEL,),
        ).fetchall()

        leaders: dict[str, str] = {}
        for cat, model, brier in rows:
            if cat not in leaders:
                leaders[cat] = model
        return leaders

    def _get_model_names(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT model_name FROM predictions"
        ).fetchall()
        return [r[0] for r in rows]

    def _get_categories(self, model_name: str) -> list[str]:
        rows = self._conn.execute(
            """SELECT DISTINCT m.category
               FROM predictions p
               JOIN markets m ON m.id = p.market_id
               WHERE p.model_name = ? AND m.category IS NOT NULL""",
            (model_name,),
        ).fetchall()
        return [r[0] for r in rows]

    def _load_resolved_predictions(
        self,
        model_name: str,
        category: str | None = None,
        since: datetime | None = None,
    ) -> list[tuple[float, int]]:
        """Return [(predicted_prob, actual_binary), ...] for resolved markets.

        Excludes void outcomes. actual_binary = 1 if outcome=yes, else 0.
        """
        sql = """
            SELECT p.probability,
                   CASE WHEN o.outcome = 'yes' THEN 1 ELSE 0 END
            FROM predictions p
            JOIN outcomes o ON o.market_id = p.market_id
            JOIN markets m ON m.id = p.market_id
            WHERE p.model_name = ? AND o.outcome != 'void'
        """
        params: list[object] = [model_name]

        if category is not None:
            sql += " AND m.category = ?"
            params.append(category)

        if since is not None:
            sql += " AND p.created_at >= ?"
            params.append(since.isoformat())

        return self._conn.execute(sql, params).fetchall()

    @staticmethod
    def _brier_score(predictions: list[tuple[float, int]]) -> float:
        total = sum((prob - actual) ** 2 for prob, actual in predictions)
        return total / len(predictions)

    @staticmethod
    def _calibration_error(predictions: list[tuple[float, int]]) -> float:
        """Equal-width decile calibration error."""
        n_bins = 10
        bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]

        for prob, actual in predictions:
            idx = min(int(prob * n_bins), n_bins - 1)
            bins[idx].append((prob, actual))

        total_error = 0.0
        populated = 0
        for b in bins:
            if not b:
                continue
            avg_pred = sum(p for p, _ in b) / len(b)
            avg_actual = sum(a for _, a in b) / len(b)
            total_error += abs(avg_pred - avg_actual)
            populated += 1

        return total_error / populated if populated > 0 else 0.0

    @staticmethod
    def _calibration_plot_data(
        predictions: list[tuple[float, int]],
    ) -> list[tuple[float, float, int]]:
        """Return [(bin_midpoint, observed_freq, count), ...] for plotting."""
        n_bins = 10
        bins: list[list[tuple[float, int]]] = [[] for _ in range(n_bins)]

        for prob, actual in predictions:
            idx = min(int(prob * n_bins), n_bins - 1)
            bins[idx].append((prob, actual))

        data = []
        for i, b in enumerate(bins):
            if not b:
                continue
            midpoint = (i + 0.5) / n_bins
            observed = sum(a for _, a in b) / len(b)
            data.append((midpoint, observed, len(b)))
        return data

    def _earliest_prediction_date(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT MIN(created_at) FROM predictions"
        ).fetchone()
        if row and row[0]:
            return datetime.fromisoformat(row[0])
        return None

    def _write_scores(
        self,
        model_name: str,
        category: str,
        period_type: str,
        brier: float,
        cal_err: float,
        n: int,
        period_start: datetime,
        period_end: datetime,
    ) -> None:
        self._conn.execute(
            """INSERT INTO model_scores
               (model_name, category, period_type, brier_score, calibration_error,
                num_predictions, period_start, period_end)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(model_name, category, period_type, period_end)
               DO UPDATE SET brier_score = excluded.brier_score,
                             calibration_error = excluded.calibration_error,
                             num_predictions = excluded.num_predictions,
                             period_start = excluded.period_start""",
            (
                model_name,
                category,
                period_type,
                brier,
                cal_err,
                n,
                period_start.isoformat(),
                period_end.isoformat(),
            ),
        )
        self._conn.commit()
