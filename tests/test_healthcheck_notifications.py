"""Tests for healthcheck Slack/plain notification formatting."""
from __future__ import annotations

from lloyd_healthcheck import (
    _format_plain_notification,
    _format_slack_notification,
)


def _sample_escalation() -> dict:
    return {
        "run_id": "run_20260529T202326_33f7b5d5",
        "timestamp_utc": "2026-05-29T20:23:30+00:00",
        "environment": "production",
        "type": "escalation",
        "incident_id": "eb4c39356aab",
        "severity": "high",
        "confidence": "high",
        "risk_tags": ["functionality"],
        "evidence": ["Last prediction 5.2h ago (threshold 4h)"],
        "recommended_safest_action": "Investigate pipeline_stuck before applying production changes",
        "alternative_actions": [
            "Increase observation window and re-check in next run (lower risk, slower response)",
            "Apply targeted mitigation immediately (faster, higher regression risk)",
        ],
        "decision_needed_by": "2026-05-29T22:23:30+00:00",
        "action_required": "yes",
    }


def test_slack_escalation_uses_blocks_not_raw_json() -> None:
    body = _format_slack_notification(_sample_escalation())
    assert "blocks" in body
    assert "text" in body
    assert "Last prediction 5.2h ago" in body["text"]
    assert "pipeline_stuck" in body["text"] or any(
        "pipeline_stuck" in str(block) for block in body["blocks"]
    )
    assert "{" not in body["text"]


def test_slack_routine_digest_pass() -> None:
    payload = {
        "run_id": "run_x",
        "timestamp_utc": "2026-05-29T00:00:00+00:00",
        "environment": "production",
        "type": "routine_digest",
        "checks_run": ["/health", "/api/data"],
        "status_summary": "pass",
        "changes_since_last_run": ["All checks healthy", "No action needed"],
        "action_required": "no",
    }
    body = _format_slack_notification(payload)
    assert "PASS" in body["text"]
    assert "blocks" in body


def test_plain_notification_escalation_readable() -> None:
    text = _format_plain_notification(_sample_escalation())
    assert "Lloyd Escalation" in text
    assert "Last prediction 5.2h ago" in text
    assert "Recommended:" in text
    assert "{" not in text
