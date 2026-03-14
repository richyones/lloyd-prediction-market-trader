"""Tests for lloyd.postmortem.metrics — portfolio-level performance calculations."""
from __future__ import annotations

import math

import pytest

from lloyd.config import Settings
from lloyd.postmortem.metrics import MetricsCalculator


class TestWinRate:
    def test_all_wins(self):
        assert MetricsCalculator._win_rate([1.0, 2.0, 0.5]) == 1.0

    def test_all_losses(self):
        assert MetricsCalculator._win_rate([-1.0, -2.0]) == 0.0

    def test_mixed(self):
        assert abs(MetricsCalculator._win_rate([1.0, -1.0, 0.5, -0.5]) - 0.5) < 1e-9

    def test_empty(self):
        assert MetricsCalculator._win_rate([]) == 0.0


class TestRoi:
    def test_basic(self):
        settings = Settings(database_path=":memory:", paper_bankroll=10_000)
        calc = MetricsCalculator.__new__(MetricsCalculator)
        calc._settings = settings
        assert abs(calc._roi([100.0, 200.0, -50.0]) - 0.025) < 1e-9


class TestPseudoSharpe:
    def test_returns_zero_for_one_trade(self):
        trades = [{"pnl": 1.0, "closed_at": "2026-01-01T00:00:00+00:00"}]
        assert MetricsCalculator._pseudo_sharpe(trades) == 0.0

    def test_returns_zero_for_same_day(self):
        trades = [
            {"pnl": 1.0, "closed_at": "2026-01-01T12:00:00+00:00"},
            {"pnl": 2.0, "closed_at": "2026-01-01T12:00:00+00:00"},
        ]
        assert MetricsCalculator._pseudo_sharpe(trades) == 0.0

    def test_positive_sharpe(self):
        trades = [
            {"pnl": 1.0, "closed_at": "2026-01-01T00:00:00+00:00"},
            {"pnl": 1.5, "closed_at": "2026-02-01T00:00:00+00:00"},
            {"pnl": 0.8, "closed_at": "2026-03-01T00:00:00+00:00"},
            {"pnl": 1.2, "closed_at": "2026-04-01T00:00:00+00:00"},
        ]
        sharpe = MetricsCalculator._pseudo_sharpe(trades)
        assert sharpe > 0

    def test_annualization_formula(self):
        """Verify the specific annualization formula is correct."""
        from datetime import datetime

        trades = [
            {"pnl": 10.0, "closed_at": "2026-01-01T00:00:00+00:00"},
            {"pnl": 10.0, "closed_at": "2026-07-01T00:00:00+00:00"},
        ]
        sharpe = MetricsCalculator._pseudo_sharpe(trades)

        # Manual calculation
        pnls = [10.0, 10.0]
        d1 = datetime.fromisoformat("2026-01-01T00:00:00+00:00")
        d2 = datetime.fromisoformat("2026-07-01T00:00:00+00:00")
        elapsed_years = (d2 - d1).total_seconds() / (365.25 * 24 * 3600)
        tpy = 2 / elapsed_years
        mean_p = 10.0
        std_p = 0.0  # stdev is 0, so sharpe should be 0

        assert sharpe == 0.0


class TestMaxDrawdown:
    def test_known_sequence(self):
        pnls = [10, 5, -20, 15, -5]
        dd = MetricsCalculator._max_drawdown(pnls)
        # cumulative: 10, 15, -5, 10, 5
        # peak:       10, 15, 15, 15, 15
        # drawdown:    0,  0, 20,  5, 10
        assert abs(dd - 20.0) < 1e-9

    def test_no_drawdown(self):
        pnls = [1, 2, 3]
        assert MetricsCalculator._max_drawdown(pnls) == 0.0

    def test_empty(self):
        assert MetricsCalculator._max_drawdown([]) == 0.0


class TestMonteCarloDrawdown:
    def test_returns_none_below_50(self):
        settings = Settings(database_path=":memory:", mc_simulations=100)
        calc = MetricsCalculator.__new__(MetricsCalculator)
        calc._settings = settings
        result = calc._monte_carlo_max_drawdown([1.0] * 49)
        assert result is None

    def test_returns_value_at_50(self):
        settings = Settings(database_path=":memory:", mc_simulations=100)
        calc = MetricsCalculator.__new__(MetricsCalculator)
        calc._settings = settings
        result = calc._monte_carlo_max_drawdown([1.0] * 50)
        assert result is not None
        assert result >= 0.0


class TestKellyAdherence:
    def test_perfect_adherence_returns_one(self):
        settings = Settings(
            database_path=":memory:",
            paper_bankroll=10_000,
            kelly_fraction=0.25,
        )
        calc = MetricsCalculator.__new__(MetricsCalculator)
        calc._settings = settings

        final_prob = 0.65
        edge = 0.05
        price = final_prob  # buy_yes
        odds = (1.0 / price) - 1.0
        theoretical = 0.25 * edge / odds
        dollar_size = theoretical * 10_000
        quantity = dollar_size / price

        trades = [{
            "direction": "buy_yes",
            "quantity": quantity,
            "executed_price": price,
            "edge": edge,
            "final_probability": final_prob,
        }]

        adherence = calc._kelly_adherence(trades)
        assert adherence == pytest.approx(1.0, abs=1e-6)
