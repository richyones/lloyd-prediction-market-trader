"""Tests for lloyd.postmortem.go_live_check — go/no-go evaluation."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from lloyd.config import Settings
from lloyd.db import init_db
from lloyd.postmortem.calibration import OVERALL_CATEGORY_SENTINEL
from lloyd.postmortem.go_live_check import CriterionResult, GoLiveChecker


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_path=":memory:",
        min_brier_sample=3,
        paper_bankroll=10_000,
        scan_interval_minutes=30,
        stability_window_days=30,
        stability_min_cycle_pct=0.95,
        mc_simulations=100,
    )


def _seed_brier(conn, model_name, brier, period_type="alltime"):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """INSERT INTO model_scores
           (model_name, category, period_type, brier_score, calibration_error,
            num_predictions, period_start, period_end)
           VALUES (?, ?, ?, ?, 0.05, 50, ?, ?)""",
        (model_name, OVERALL_CATEGORY_SENTINEL, period_type, brier, now, now),
    )
    conn.commit()


def _seed_settled_trade(conn, pnl, market_id=None, direction="buy_yes",
                        executed_price=0.60, quantity=10.0, fee=0.006,
                        opened_at=None, closed_at=None):
    now = datetime.now(timezone.utc)
    if opened_at is None:
        opened_at = (now - timedelta(days=1)).isoformat()
    if closed_at is None:
        closed_at = now.isoformat()

    if market_id is None:
        cur = conn.execute(
            """INSERT INTO markets (platform, platform_id, question, current_price, volume, fetched_at)
               VALUES ('polymarket', ?, 'Test', 0.5, 10000, ?)""",
            (f"pm-{now.timestamp()}", now.isoformat()),
        )
        market_id = cur.lastrowid

    ep_cur = conn.execute(
        """INSERT INTO ensemble_predictions
           (market_id, ensemble_probability, market_price, edge, final_probability,
            model_predictions, trade_signal, created_at)
           VALUES (?, 0.65, 0.60, 0.05, 0.62, '[]', 'buy_yes', ?)""",
        (market_id, now.isoformat()),
    )
    ep_id = ep_cur.lastrowid

    conn.execute(
        """INSERT INTO trades
           (market_id, ensemble_prediction_id, platform, direction, quantity,
            limit_price, executed_price, slippage, fee, is_paper, status, opened_at, closed_at, pnl)
           VALUES (?, ?, 'polymarket', ?, ?, ?, ?, 0.0, ?, 1, 'settled', ?, ?, ?)""",
        (market_id, ep_id, direction, quantity, executed_price, executed_price, fee,
         opened_at, closed_at, pnl),
    )
    conn.commit()
    return market_id


def _seed_ensemble_calibration(conn, final_probs_and_outcomes):
    """Seed ensemble_predictions and outcomes for calibration check.

    Each (final_prob, outcome_str) pair gets its own unique market.
    """
    now = datetime.now(timezone.utc)
    for i, (fp, outcome) in enumerate(final_probs_and_outcomes):
        ts = (now + timedelta(seconds=i)).isoformat()
        cur = conn.execute(
            """INSERT INTO markets (platform, platform_id, question, current_price, volume, fetched_at)
               VALUES ('polymarket', ?, 'CalTest', 0.5, 10000, ?)""",
            (f"pm-cal-{i}-{now.timestamp()}", ts),
        )
        market_id = cur.lastrowid

        conn.execute(
            """INSERT INTO ensemble_predictions
               (market_id, ensemble_probability, market_price, edge, final_probability,
                model_predictions, trade_signal, created_at)
               VALUES (?, ?, 0.5, 0.0, ?, '[]', 'no_trade', ?)""",
            (market_id, fp, fp, ts),
        )

        conn.execute(
            "INSERT INTO outcomes (market_id, platform, outcome, resolved_at) VALUES (?, 'polymarket', ?, ?)",
            (market_id, outcome, ts),
        )

    conn.commit()


class TestCheckBrier:
    def test_good_brier_passes(self, db, settings):
        _seed_brier(db, "gemini", 0.15)
        checker = GoLiveChecker(db, settings)
        result = checker._check_brier()
        assert result.passed is True
        assert result.value == 0.15

    def test_bad_brier_fails(self, db, settings):
        _seed_brier(db, "gemini", 0.30)
        checker = GoLiveChecker(db, settings)
        result = checker._check_brier()
        assert result.passed is False

    def test_no_data(self, db, settings):
        checker = GoLiveChecker(db, settings)
        result = checker._check_brier()
        assert result.passed is False
        assert result.value is None


class TestCheckRoi:
    def test_positive_roi_passes(self, db, settings):
        for _ in range(5):
            _seed_settled_trade(db, pnl=50.0)
        checker = GoLiveChecker(db, settings)
        result = checker._check_roi()
        assert result.passed is True

    def test_negative_roi_fails(self, db, settings):
        for _ in range(5):
            _seed_settled_trade(db, pnl=-50.0)
        checker = GoLiveChecker(db, settings)
        result = checker._check_roi()
        assert result.passed is False


class TestCheckCalibrationError:
    def test_well_calibrated_passes(self, db, settings):
        data = [(0.02, "no")] * 5 + [(0.98, "yes")] * 5
        _seed_ensemble_calibration(db, data)
        checker = GoLiveChecker(db, settings)
        result = checker._check_calibration_error()
        assert result.passed is True
        assert result.value is not None
        assert result.value <= 0.05

    def test_badly_calibrated_fails(self, db, settings):
        data = [(0.9, "no")] * 10
        _seed_ensemble_calibration(db, data)
        checker = GoLiveChecker(db, settings)
        result = checker._check_calibration_error()
        assert result.passed is False

    def test_insufficient_data(self, db, settings):
        _seed_ensemble_calibration(db, [(0.5, "yes")])
        checker = GoLiveChecker(db, settings)
        result = checker._check_calibration_error()
        assert result.value is None


class TestCheckSampleSize:
    def test_enough_trades_passes(self, db, settings):
        for _ in range(120):
            _seed_settled_trade(db, pnl=1.0)
        checker = GoLiveChecker(db, settings)
        result = checker._check_sample_size()
        assert result.passed is True

    def test_too_few_fails(self, db, settings):
        for _ in range(80):
            _seed_settled_trade(db, pnl=1.0)
        checker = GoLiveChecker(db, settings)
        result = checker._check_sample_size()
        assert result.passed is False


class TestCheckDrawdown:
    def test_insufficient_data_returns_none(self, db, settings):
        for _ in range(10):
            _seed_settled_trade(db, pnl=1.0)
        checker = GoLiveChecker(db, settings)
        result = checker._check_drawdown()
        assert result.value is None


class TestCheckStability:
    def test_sufficient_cycles_passes(self, db, settings):
        now = datetime.now(timezone.utc)
        expected = int((30 * 24 * 60) / 30)
        for i in range(expected):
            ts = (now - timedelta(minutes=30 * i)).isoformat()
            db.execute(
                "INSERT INTO scan_results (market_id, exploitability_score, scan_timestamp) VALUES (NULL, 0.5, ?)",
                (ts,),
            )
        db.commit()

        checker = GoLiveChecker(db, settings)
        result = checker._check_stability()
        assert result.passed is True

    def test_insufficient_cycles_fails(self, db, settings):
        checker = GoLiveChecker(db, settings)
        result = checker._check_stability()
        assert result.passed is False


class TestWeakestCriteria:
    def test_insufficient_data_excluded_from_weakest(self):
        """Criteria with value=None should not appear in weakest list."""
        criteria = [
            CriterionResult(name="drawdown", passed=False, value=None, threshold=3000.0),
            CriterionResult(name="brier", passed=True, value=0.15, threshold=0.20),
        ]
        weakest = GoLiveChecker._weakest_criteria(criteria)
        assert len(weakest) == 0

    def test_failing_criteria_ranked(self):
        criteria = [
            CriterionResult(name="brier", passed=False, value=0.35, threshold=0.20),
            CriterionResult(name="roi", passed=False, value=-0.05, threshold=0.0),
        ]
        weakest = GoLiveChecker._weakest_criteria(criteria)
        assert len(weakest) == 2
        assert weakest[0].name in ("brier", "roi")


class TestGoLiveAllPass:
    """Test that go=True requires ALL criteria to pass with measurable values."""

    def test_go_requires_all_criteria_pass(self, db, settings):
        _seed_brier(db, "gemini", 0.15)

        for i in range(120):
            ts_open = (datetime.now(timezone.utc) - timedelta(days=180 - i)).isoformat()
            ts_close = (datetime.now(timezone.utc) - timedelta(days=179 - i)).isoformat()
            _seed_settled_trade(db, pnl=10.0, opened_at=ts_open, closed_at=ts_close)

        data = [(0.02, "no")] * 10 + [(0.98, "yes")] * 10
        _seed_ensemble_calibration(db, data)

        now = datetime.now(timezone.utc)
        expected_cycles = int((30 * 24 * 60) / 30)
        for i in range(expected_cycles):
            ts = (now - timedelta(minutes=30 * i)).isoformat()
            db.execute(
                "INSERT INTO scan_results (market_id, exploitability_score, scan_timestamp) VALUES (NULL, 0.5, ?)",
                (ts,),
            )
        db.commit()

        checker = GoLiveChecker(db, settings)
        result = checker.run()

        # Verify each criterion individually
        by_name = {c.name: c for c in result.criteria}

        assert by_name["brier_score"].passed is True
        assert by_name["roi"].passed is True
        assert by_name["calibration_error"].passed is True
        assert by_name["sample_size"].passed is True
        assert by_name["stability"].passed is True

        # Drawdown needs 50+ trades for MC; we have 120 so it should have a value
        if by_name["max_drawdown"].value is not None:
            # All P&Ls are +10, so max drawdown is 0, well under threshold
            assert by_name["max_drawdown"].passed is True
            assert result.go is True
        else:
            # If MC somehow didn't produce a value, go should be False
            assert result.go is False
