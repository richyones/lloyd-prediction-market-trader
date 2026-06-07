"""Tests for pipeline_stuck threshold vs configured prediction interval."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import lloyd_healthcheck as hc


def _data_with_prediction_age(hours: float, interval_hours: float = 6) -> dict:
    created = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    return {
        "scheduler": {"prediction_interval_hours": interval_hours},
        "recent_predictions": [{"created_at": created}],
    }


def test_pipeline_stuck_not_fired_within_scheduled_interval() -> None:
    """6h interval + 1h grace: 4.5h since last prediction is healthy."""
    finding = hc.check_pipeline_stuck(_data_with_prediction_age(4.5, interval_hours=6))
    assert finding is None


def test_pipeline_stuck_fired_after_interval_plus_grace() -> None:
    finding = hc.check_pipeline_stuck(_data_with_prediction_age(8.0, interval_hours=6))
    assert finding is not None
    assert finding["check_name"] == "pipeline_stuck"
    assert "8.0h" in finding["detail"]


def test_scan_dead_uses_double_interval() -> None:
    finding = hc.check_scan_dead(_data_with_prediction_age(8.0, interval_hours=6))
    assert finding is None
    finding = hc.check_scan_dead(_data_with_prediction_age(13.0, interval_hours=6))
    assert finding is not None
