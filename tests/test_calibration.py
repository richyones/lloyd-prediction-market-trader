"""Tests for lloyd.postmortem.calibration — Brier scores, calibration, model weights."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from lloyd.config import Settings
from lloyd.db import init_db
from lloyd.postmortem.calibration import CalibrationAnalyzer, OVERALL_CATEGORY_SENTINEL


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def settings() -> Settings:
    return Settings(database_path=":memory:", min_brier_sample=3)


def _seed_prediction(conn, market_id, model_name, probability, created_at=None):
    if created_at is None:
        created_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO predictions
           (market_id, model_name, probability, confidence, reasoning,
            tokens_used, cost_usd, prompt_version, context_quality, created_at)
           VALUES (?, ?, ?, 5, '', 100, 0.01, 'v1', 'good', ?)""",
        (market_id, model_name, probability, created_at),
    )
    conn.commit()


def _seed_market_and_outcome(conn, platform_id, outcome, category=None):
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO markets (platform, platform_id, question, category, current_price, volume, fetched_at)
           VALUES ('polymarket', ?, 'Test', ?, 0.5, 10000, ?)""",
        (platform_id, category, now),
    )
    market_id = cur.lastrowid
    conn.execute(
        "INSERT INTO outcomes (market_id, platform, outcome, resolved_at) VALUES (?, 'polymarket', ?, ?)",
        (market_id, outcome, now),
    )
    conn.commit()
    return market_id


class TestBrierScore:
    def test_perfect_prediction(self, db, settings):
        # Predicted 1.0, actual 1 → (1-1)^2 = 0; Predicted 0.0, actual 0 → (0-0)^2 = 0
        preds = [(1.0, 1), (0.0, 0), (1.0, 1)]
        assert CalibrationAnalyzer._brier_score(preds) == 0.0

    def test_worst_prediction(self, db, settings):
        preds = [(0.0, 1), (1.0, 0), (0.0, 1)]
        assert CalibrationAnalyzer._brier_score(preds) == 1.0

    def test_known_value(self, db, settings):
        # Predicted 0.7, actual 1 → (0.7-1)^2 = 0.09
        # Predicted 0.3, actual 0 → (0.3-0)^2 = 0.09
        preds = [(0.7, 1), (0.3, 0)]
        assert abs(CalibrationAnalyzer._brier_score(preds) - 0.09) < 1e-9


class TestCalibrationError:
    def test_perfect_calibration(self, db, settings):
        preds = [(0.05, 0)] * 10 + [(0.95, 1)] * 10
        cal = CalibrationAnalyzer._calibration_error(preds)
        assert cal < 0.1

    def test_miscalibrated(self, db, settings):
        # All predicted ~0.9 but actual is 0 → very miscalibrated
        preds = [(0.9, 0)] * 20
        cal = CalibrationAnalyzer._calibration_error(preds)
        assert cal > 0.5


class TestModelWeights:
    def test_inverse_proportionality(self, db, settings):
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO model_scores
               (model_name, category, period_type, brier_score, calibration_error,
                num_predictions, period_start, period_end)
               VALUES ('gemini', ?, 'alltime', 0.10, 0.05, 20, ?, ?)""",
            (OVERALL_CATEGORY_SENTINEL, now, now),
        )
        db.execute(
            """INSERT INTO model_scores
               (model_name, category, period_type, brier_score, calibration_error,
                num_predictions, period_start, period_end)
               VALUES ('gpt5', ?, 'alltime', 0.20, 0.08, 20, ?, ?)""",
            (OVERALL_CATEGORY_SENTINEL, now, now),
        )
        db.commit()

        analyzer = CalibrationAnalyzer(db, settings)
        weights = analyzer.get_model_weights()

        assert weights is not None
        assert weights["gemini"] > weights["gpt5"]
        assert abs(sum(weights.values()) - 1.0) < 1e-9

    def test_returns_none_with_one_model(self, db, settings):
        now = datetime.now(timezone.utc).isoformat()
        db.execute(
            """INSERT INTO model_scores
               (model_name, category, period_type, brier_score, num_predictions, period_start, period_end)
               VALUES ('gemini', ?, 'alltime', 0.10, 20, ?, ?)""",
            (OVERALL_CATEGORY_SENTINEL, now, now),
        )
        db.commit()

        analyzer = CalibrationAnalyzer(db, settings)
        assert analyzer.get_model_weights() is None


class TestMinBrierSample:
    def test_skips_below_threshold(self, db, settings):
        mid = _seed_market_and_outcome(db, "pm-few", "yes")
        _seed_prediction(db, mid, "gemini", 0.7)  # only 1, below min_brier_sample=3

        analyzer = CalibrationAnalyzer(db, settings)
        analyzer.run()

        row = db.execute("SELECT COUNT(*) FROM model_scores").fetchone()
        assert row[0] == 0


class TestVoidExclusion:
    def test_void_not_counted(self, db, settings):
        mid = _seed_market_and_outcome(db, "pm-void", "void")
        for i in range(5):
            _seed_prediction(db, mid, "gemini", 0.5)

        analyzer = CalibrationAnalyzer(db, settings)
        preds = analyzer._load_resolved_predictions("gemini")
        assert len(preds) == 0


class TestUniqueConstraint:
    def test_upsert_on_repeat(self, db, settings):
        now = datetime.now(timezone.utc)
        analyzer = CalibrationAnalyzer(db, settings)

        analyzer._write_scores("gemini", OVERALL_CATEGORY_SENTINEL, "alltime", 0.20, 0.05, 10, now, now)
        analyzer._write_scores("gemini", OVERALL_CATEGORY_SENTINEL, "alltime", 0.15, 0.04, 15, now, now)

        rows = db.execute(
            "SELECT brier_score, num_predictions FROM model_scores WHERE model_name = 'gemini' AND period_type = 'alltime'"
        ).fetchall()
        assert len(rows) == 1
        assert abs(rows[0][0] - 0.15) < 1e-9
        assert rows[0][1] == 15
