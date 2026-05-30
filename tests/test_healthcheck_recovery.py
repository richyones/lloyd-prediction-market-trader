"""Tests for healthcheck incident state and recovery notifications."""
from __future__ import annotations

from pathlib import Path

import lloyd_healthcheck as hc


def test_detect_recoveries_only_for_evaluated_checks(tmp_path: Path) -> None:
    state = {
        "open_incidents": {
            "pipeline_stuck": {
                "check_name": "pipeline_stuck",
                "incident_id": "abc123",
                "severity": "high",
                "detail": "Last prediction 5.2h ago",
                "alert_type": "escalation",
            },
            "cost_spike": {
                "check_name": "cost_spike",
                "incident_id": "def456",
                "severity": "warning",
                "detail": "cycle $2.50",
                "alert_type": "autotriage",
            },
        }
    }
    recoveries = hc.detect_recoveries(
        state,
        checks_evaluated={"pipeline_stuck", "health_endpoint"},
        failing_check_names=set(),
    )
    assert len(recoveries) == 1
    assert recoveries[0]["check_name"] == "pipeline_stuck"
    assert "cost_spike" in state["open_incidents"]


def test_record_and_save_state_roundtrip(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state.json"
    monkeypatch.setattr(hc, "STATE_PATH", str(state_path))

    state = hc.load_state()
    finding = hc._make_finding(
        "pipeline_stuck",
        "high",
        "high",
        ["functionality"],
        "Last prediction 5.2h ago (threshold 4h)",
    )
    hc.record_open_incident(state, finding, "escalation", "run_test")
    hc.save_state(state)

    loaded = hc.load_state()
    assert "pipeline_stuck" in loaded["open_incidents"]
    assert loaded["open_incidents"]["pipeline_stuck"]["alert_type"] == "escalation"


def test_build_recovery_report_contract() -> None:
    open_inc = {
        "check_name": "pipeline_stuck",
        "incident_id": "eb4c39356aab",
        "severity": "high",
        "detail": "Last prediction 5.2h ago (threshold 4h)",
        "alert_type": "escalation",
    }
    payload = hc.build_recovery_report("run_x", open_inc)
    assert payload["type"] == "recovery_report"
    assert payload["result"] == "recovered"
    assert payload["check_name"] == "pipeline_stuck"
    assert payload["incident_id"] == "eb4c39356aab"
    assert "first clean run" in payload["verification"]


def test_slack_recovery_is_readable() -> None:
    payload = hc.build_recovery_report(
        "run_x",
        {
            "check_name": "pipeline_stuck",
            "incident_id": "eb4c39356aab",
            "severity": "high",
            "detail": "Last prediction 5.2h ago (threshold 4h)",
            "alert_type": "escalation",
        },
    )
    body = hc._format_slack_notification(payload)
    assert "blocks" in body
    assert "Recovered" in body["text"]
    assert "{" not in body["text"]
