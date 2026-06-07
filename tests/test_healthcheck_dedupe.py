"""Tests for healthcheck escalation deduplication."""
from __future__ import annotations

import lloyd_healthcheck as hc


def test_should_notify_escalation_false_when_already_open() -> None:
    state = {
        "open_incidents": {
            "resolver_overdue": {
                "check_name": "resolver_overdue",
                "alert_type": "escalation",
            }
        }
    }
    finding = {"check_name": "resolver_overdue", "incident_id": "abc"}
    assert hc.should_notify_escalation(state, finding) is False


def test_should_notify_escalation_true_for_new_check() -> None:
    state: dict = {"open_incidents": {}}
    finding = {"check_name": "pipeline_stuck", "incident_id": "xyz"}
    assert hc.should_notify_escalation(state, finding) is True
