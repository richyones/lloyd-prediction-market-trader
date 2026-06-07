"""Tests for Kalshi API-first resolution helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from lloyd.postmortem.kalshi_resolution import (
    infer_outcome_from_yes_price,
    parse_settled_outcome,
    parse_settlement_value_outcome,
    resolve_kalshi_outcome,
)


def test_parse_settled_outcome_yes() -> None:
    assert parse_settled_outcome({"status": "settled", "result": "yes"}) == "yes"


def test_finalized_settlement_value_dollars() -> None:
    assert (
        parse_settlement_value_outcome({"settlement_value_dollars": "1.0000"})
        == "yes"
    )
    assert (
        parse_settlement_value_outcome({"settlement_value_dollars": "0.0000"})
        == "no"
    )
    assert (
        parse_settled_outcome(
            {
                "status": "finalized",
                "result": "",
                "settlement_value_dollars": "0.0000",
                "settlement_ts": "2026-06-01T12:00:00Z",
            }
        )
        == "no"
    )


def test_determined_result_before_finalized() -> None:
    assert (
        parse_settled_outcome({"status": "determined", "result": "no"})
        == "no"
    )


def test_api_price_beats_stale_db_price() -> None:
    past = datetime(2026, 6, 1, tzinfo=timezone.utc)
    market_data = {
        "status": "closed",
        "result": "",
        "last_price_dollars": "0.02",
        "close_time": past.isoformat(),
    }
    outcome, method, debug = resolve_kalshi_outcome(
        market_data,
        close_date=past,
        db_yes_price=0.35,
        now_utc=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    assert outcome == "no"
    assert method == "api_price_fallback"
    assert debug["api_yes_price"] == 0.02


def test_stale_db_not_used_when_api_has_mid_price() -> None:
    past = datetime(2026, 6, 1, tzinfo=timezone.utc)
    market_data = {
        "status": "closed",
        "last_price_dollars": "0.12",
        "close_time": past.isoformat(),
    }
    outcome, method, _ = resolve_kalshi_outcome(
        market_data,
        close_date=past,
        db_yes_price=0.02,
        now_utc=datetime(2026, 6, 6, tzinfo=timezone.utc),
    )
    assert outcome is None
    assert method is None


def test_infer_outcome_thresholds() -> None:
    assert infer_outcome_from_yes_price(0.05) == "no"
    assert infer_outcome_from_yes_price(0.06) is None
    assert infer_outcome_from_yes_price(0.95) == "yes"
