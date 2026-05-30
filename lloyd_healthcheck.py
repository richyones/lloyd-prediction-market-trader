"""Contract-based health and triage monitor for Lloyd deployment."""
from __future__ import annotations

import hashlib
import json
import os
import random
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def _normalize_base_url(url: str) -> str:
    """Strip trailing slashes and accidental /health or /api/data path suffixes."""
    normalized = url.strip().rstrip("/")
    for suffix in ("/health", "/api/data"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)].rstrip("/")
    return normalized


_BASE_URL: str = ""


def _base_url() -> str:
    global _BASE_URL
    if not _BASE_URL:
        _BASE_URL = _normalize_base_url(os.environ["LLOYD_BASE_URL"])
    return _BASE_URL


ENVIRONMENT = os.environ.get("LLOYD_ENVIRONMENT", "production")

MAX_COST_PER_CYCLE_USD = float(os.environ.get("MAX_COST_PER_CYCLE_USD", "2.0"))
PIPELINE_STUCK_HOURS = int(os.environ.get("PIPELINE_STUCK_HOURS", "4"))
SCAN_DEAD_HOURS = int(os.environ.get("SCAN_DEAD_HOURS", "6"))
RESOLVER_LOOKBACK_DAYS = int(os.environ.get("RESOLVER_LOOKBACK_DAYS", "3"))
RESOLVER_HIGH_COUNT = int(os.environ.get("RESOLVER_HIGH_COUNT", "5"))

CYCLE_COST_HIGH_USD = float(os.environ.get("CYCLE_COST_HIGH_USD", "3.0"))
CYCLE_COST_CRITICAL_USD = float(os.environ.get("CYCLE_COST_CRITICAL_USD", "5.0"))
DAILY_COST_HIGH_USD = float(os.environ.get("DAILY_COST_HIGH_USD", "20.0"))
DAILY_COST_CRITICAL_USD = float(os.environ.get("DAILY_COST_CRITICAL_USD", "35.0"))

DAILY_COST_WARN_MULTIPLIER = float(os.environ.get("DAILY_COST_WARN_MULTIPLIER", "1.25"))
DAILY_COST_HIGH_MULTIPLIER = float(os.environ.get("DAILY_COST_HIGH_MULTIPLIER", "1.5"))
DAILY_COST_CRITICAL_MULTIPLIER = float(os.environ.get("DAILY_COST_CRITICAL_MULTIPLIER", "2.0"))
API_DATA_FETCH_ATTEMPTS = int(os.environ.get("API_DATA_FETCH_ATTEMPTS", "2"))
API_DATA_RETRY_BACKOFF_SECONDS = float(os.environ.get("API_DATA_RETRY_BACKOFF_SECONDS", "2.0"))

SEVERITY_ORDER = {"info": 0, "warning": 1, "high": 2, "critical": 3}

HEALTH_FETCH_ATTEMPTS = int(os.environ.get("HEALTH_FETCH_ATTEMPTS", "3"))
HEALTH_FETCH_TIMEOUT_SECONDS = float(os.environ.get("HEALTH_FETCH_TIMEOUT_SECONDS", "30"))

STATE_PATH = os.environ.get("HEALTHCHECK_STATE_PATH", ".healthcheck-state.json")


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _run_id() -> str:
    return f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{uuid.uuid4().hex[:8]}"


def _incident_id(check_name: str, detail: str) -> str:
    basis = f"{check_name}|{detail[:160]}"
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


def _send_slack(body: dict[str, Any]) -> None:
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not slack_webhook_url:
        print("[notify] slack webhook not configured")
        return
    try:
        resp = httpx.post(slack_webhook_url, json=body, timeout=10)
        resp.raise_for_status()
        print("[notify] slack delivered")
    except Exception as exc:
        print(f"[notify] slack delivery failed: {exc}")


def _send_telegram(message: str) -> None:
    telegram_bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not telegram_bot_token or not telegram_chat_id:
        print("[notify] telegram not configured")
        return
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{telegram_bot_token}/sendMessage",
            json={"chat_id": telegram_chat_id, "text": message},
            timeout=10,
        )
        resp.raise_for_status()
        print("[notify] telegram delivered")
    except Exception as exc:
        print(f"[notify] telegram delivery failed: {exc}")


def _footer_line(payload: dict[str, Any]) -> str:
    parts = [
        payload.get("run_id", ""),
        payload.get("environment", ""),
        payload.get("timestamp_utc", ""),
    ]
    incident = payload.get("incident_id")
    if incident:
        parts.insert(0, f"incident `{incident}`")
    return " · ".join(p for p in parts if p)


def _bullet_lines(items: list[str]) -> str:
    return "\n".join(f"• {item}" for item in items)


def _format_slack_notification(payload: dict[str, Any], *, immediate: bool = False) -> dict[str, Any]:
    """Human-readable Slack webhook body (text fallback + Block Kit blocks)."""
    msg_type = payload.get("type")
    if msg_type == "escalation":
        return _slack_escalation(payload, immediate=immediate)
    if msg_type == "autotriage_report":
        return _slack_autotriage(payload)
    if msg_type == "routine_digest":
        return _slack_routine_digest(payload)
    if msg_type == "recovery_report":
        return _slack_recovery(payload)
    return {"text": json.dumps(payload, ensure_ascii=True)}


def _slack_escalation(payload: dict[str, Any], *, immediate: bool) -> dict[str, Any]:
    severity = str(payload.get("severity", "unknown")).upper()
    prefix = "🚨 IMMEDIATE — " if immediate else ""
    title = f"{prefix}Lloyd Escalation — {severity}"
    evidence = payload.get("evidence") or []
    evidence_text = _bullet_lines(evidence) if evidence else "_(none)_"
    risks = ", ".join(payload.get("risk_tags") or []) or "none"
    fallback = f"{title.strip()}: {evidence[0]}" if evidence else title.strip()

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title.strip(), "emoji": True}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity*\n{payload.get('severity', '?')}"},
                {"type": "mrkdwn", "text": f"*Confidence*\n{payload.get('confidence', '?')}"},
                {"type": "mrkdwn", "text": f"*Risk tags*\n{risks}"},
                {"type": "mrkdwn", "text": f"*Action required*\n{payload.get('action_required', '?')}"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Evidence*\n{evidence_text}"}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Recommended action*\n{payload.get('recommended_safest_action', '')}",
            },
        },
    ]

    alts = payload.get("alternative_actions") or []
    if alts:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Alternatives*\n{_bullet_lines(alts)}"},
            }
        )

    decision_by = payload.get("decision_needed_by")
    if decision_by:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Decision needed by*\n{decision_by}"},
            }
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _footer_line(payload)}]}
    )
    return {"text": fallback, "blocks": blocks}


def _slack_autotriage(payload: dict[str, Any]) -> dict[str, Any]:
    severity = str(payload.get("severity", "unknown")).upper()
    title = f"Lloyd Autotriage — {severity}"
    why = payload.get("why", "")
    risks = ", ".join(payload.get("risk_tags") or []) or "none"
    fallback = f"{title}: {why}" if why else title

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Severity*\n{payload.get('severity', '?')}"},
                {"type": "mrkdwn", "text": f"*Confidence*\n{payload.get('confidence', '?')}"},
                {"type": "mrkdwn", "text": f"*Risk tags*\n{risks}"},
                {"type": "mrkdwn", "text": f"*Action required*\n{payload.get('action_required', 'no')}"},
            ],
        },
    ]
    if why:
        blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Finding*\n{why}"}})

    done = payload.get("what_was_done") or []
    if done:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*What was done*\n{_bullet_lines(done)}"},
            }
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": _footer_line(payload)}]}
    )
    return {"text": fallback, "blocks": blocks}


def _slack_recovery(payload: dict[str, Any]) -> dict[str, Any]:
    check_name = payload.get("check_name", "unknown")
    title = f"✅ Lloyd Recovered — {check_name}"
    fallback = f"{title}: {payload.get('verification', '')}"

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Verification*\n{payload.get('verification', '')}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Previous severity*\n{payload.get('previous_severity', '?')}"},
                {"type": "mrkdwn", "text": f"*Previous alert*\n{payload.get('previous_alert_type', '?')}"},
            ],
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Previous issue*\n{payload.get('previous_detail', '')}"},
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _footer_line(payload)}],
        },
    ]
    return {"text": fallback, "blocks": blocks}


def _slack_routine_digest(payload: dict[str, Any]) -> dict[str, Any]:
    status = str(payload.get("status_summary", "unknown")).upper()
    emoji = "✅" if status == "PASS" else "⚠️"
    title = f"{emoji} Lloyd Health Check — {status}"
    deltas = payload.get("changes_since_last_run") or []
    checks = ", ".join(payload.get("checks_run") or [])
    delta_text = _bullet_lines(deltas) if deltas else "_(none)_"
    fallback = f"{title}: {deltas[0]}" if deltas else title

    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Summary*\n{delta_text}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Checks run*\n{checks}"},
        },
        {"type": "divider"},
        {
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": _footer_line(payload)}],
        },
    ]
    return {"text": fallback, "blocks": blocks}


def _format_plain_notification(payload: dict[str, Any], *, immediate: bool = False) -> str:
    """Plain-text notification for Telegram and log-friendly copy."""
    msg_type = payload.get("type")
    lines: list[str] = []

    if msg_type == "escalation":
        if immediate:
            lines.append("IMMEDIATE — Lloyd Escalation")
        else:
            lines.append("Lloyd Escalation")
        lines.extend(
            [
                f"Severity: {payload.get('severity', '?')} | Confidence: {payload.get('confidence', '?')}",
                f"Risk: {', '.join(payload.get('risk_tags') or [])}",
                "",
                "Evidence:",
                *([f"  - {e}" for e in payload.get("evidence") or []]),
                "",
                f"Recommended: {payload.get('recommended_safest_action', '')}",
            ]
        )
        if payload.get("decision_needed_by"):
            lines.append(f"Decision needed by: {payload['decision_needed_by']}")
    elif msg_type == "autotriage_report":
        lines.extend(
            [
                f"Lloyd Autotriage — {str(payload.get('severity', '?')).upper()}",
                f"Finding: {payload.get('why', '')}",
                f"Severity: {payload.get('severity', '?')} | Confidence: {payload.get('confidence', '?')}",
                f"Action required: {payload.get('action_required', 'no')}",
            ]
        )
    elif msg_type == "routine_digest":
        lines.extend(
            [
                f"Lloyd Health Check — {str(payload.get('status_summary', '?')).upper()}",
                *(payload.get("changes_since_last_run") or []),
                f"Checks: {', '.join(payload.get('checks_run') or [])}",
            ]
        )
    elif msg_type == "recovery_report":
        lines.extend(
            [
                f"Lloyd Recovered — {payload.get('check_name', '?')}",
                f"Verification: {payload.get('verification', '')}",
                f"Previous ({payload.get('previous_severity', '?')}): {payload.get('previous_detail', '')}",
                "Action required: no",
            ]
        )
    else:
        return json.dumps(payload, ensure_ascii=True)

    lines.append("")
    lines.append(_footer_line(payload))
    return "\n".join(lines)


def notify(payload: dict[str, Any], immediate: bool = False) -> None:
    """Deliver contract payload: JSON to stdout, formatted text to Slack/Telegram."""
    log_line = json.dumps(payload, ensure_ascii=True)
    if immediate:
        log_line = f"[IMMEDIATE] {log_line}"
    print(log_line)
    _send_slack(_format_slack_notification(payload, immediate=immediate))
    _send_telegram(_format_plain_notification(payload, immediate=immediate))


def _base_contract(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "timestamp_utc": _now_iso(),
        "environment": ENVIRONMENT,
    }


def _fetch_json_with_retries(
    client: httpx.Client,
    path: str,
    *,
    attempts: int,
    timeout_seconds: float,
    label: str,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    url = f"{_base_url()}{path}"
    tries = max(attempts, 1)
    for attempt in range(1, tries + 1):
        try:
            resp = client.get(url, timeout=timeout_seconds)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt == tries:
                break
            sleep_s = API_DATA_RETRY_BACKOFF_SECONDS * attempt + random.uniform(0, 0.5)
            print(f"[{label}] attempt {attempt}/{tries} failed: {exc}; retrying in {sleep_s:.1f}s")
            time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc


def fetch_health(client: httpx.Client) -> dict[str, Any]:
    return _fetch_json_with_retries(
        client,
        "/health",
        attempts=HEALTH_FETCH_ATTEMPTS,
        timeout_seconds=HEALTH_FETCH_TIMEOUT_SECONDS,
        label="health",
    )


def fetch_api_data(client: httpx.Client) -> dict[str, Any]:
    return _fetch_json_with_retries(
        client,
        "/api/data",
        attempts=API_DATA_FETCH_ATTEMPTS,
        timeout_seconds=90,
        label="api_data",
    )


def _make_finding(
    check_name: str,
    severity: str,
    confidence: str,
    risk_tags: list[str],
    detail: str,
) -> dict[str, Any]:
    return {
        "check_name": check_name,
        "severity": severity,
        "confidence": confidence,
        "risk_tags": risk_tags or ["none"],
        "detail": detail,
        "incident_id": _incident_id(check_name, detail),
    }


def check_resolver(data: dict[str, Any]) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RESOLVER_LOOKBACK_DAYS)
    open_trades: list[dict[str, Any]] = data.get("open_trades", [])
    if not open_trades:
        return None

    overdue: list[dict[str, str]] = []
    for trade in open_trades:
        close_date_str = trade.get("close_date")
        if not close_date_str:
            continue
        close_dt = _parse_dt(close_date_str if "T" in close_date_str else f"{close_date_str}T00:00:00")
        if close_dt and close_dt < cutoff:
            overdue.append(
                {
                    "platform": str(trade.get("platform", "?")),
                    "question": str(trade.get("question", "unknown"))[:60],
                    "close_date": close_date_str,
                }
            )

    if not overdue:
        return None

    severity = "high" if len(overdue) > RESOLVER_HIGH_COUNT else "warning"
    detail = (
        f"{len(overdue)} overdue open trade(s) beyond {RESOLVER_LOOKBACK_DAYS}d: "
        + "; ".join(f"{x['platform']}:{x['question']}({x['close_date']})" for x in overdue[:3])
    )
    return _make_finding(
        "resolver_overdue",
        severity,
        "medium",
        ["functionality"],
        detail,
    )


def _latest_prediction_age_hours(data: dict[str, Any]) -> float | None:
    recent_predictions: list[dict[str, Any]] = data.get("recent_predictions", [])
    if not recent_predictions:
        return None
    latest_str = recent_predictions[0].get("created_at", "")
    latest_dt = _parse_dt(latest_str)
    if latest_dt is None:
        return None
    return (datetime.now(timezone.utc) - latest_dt).total_seconds() / 3600


def check_pipeline_stuck(data: dict[str, Any]) -> dict[str, Any] | None:
    hours_since = _latest_prediction_age_hours(data)
    if hours_since is None:
        return _make_finding(
            "pipeline_stuck",
            "warning",
            "low",
            ["functionality"],
            "Unable to determine latest prediction timestamp",
        )
    if hours_since <= PIPELINE_STUCK_HOURS:
        return None
    severity = "high" if hours_since <= PIPELINE_STUCK_HOURS * 2 else "critical"
    return _make_finding(
        "pipeline_stuck",
        severity,
        "high",
        ["functionality"],
        f"Last prediction {hours_since:.1f}h ago (threshold {PIPELINE_STUCK_HOURS}h)",
    )


def check_scan_dead(data: dict[str, Any]) -> dict[str, Any] | None:
    hours_since = _latest_prediction_age_hours(data)
    if hours_since is None:
        return None
    if hours_since <= SCAN_DEAD_HOURS:
        return None
    severity = "high" if hours_since <= SCAN_DEAD_HOURS * 2 else "critical"
    return _make_finding(
        "scan_dead",
        severity,
        "high",
        ["functionality"],
        f"No fresh prediction in {hours_since:.1f}h (threshold {SCAN_DEAD_HOURS}h)",
    )


def check_cost_spike(data: dict[str, Any]) -> dict[str, Any] | None:
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    cost_by_day: list[dict[str, Any]] = data.get("cost_by_day", [])
    daily_map = {str(x.get("day", "")): float(x.get("daily_cost") or 0) for x in cost_by_day}
    today_cost = daily_map.get(today_str, 0.0)

    trailing_vals = [
        v
        for day, v in daily_map.items()
        if day != today_str and _parse_dt(f"{day}T00:00:00+00:00")
    ]
    baseline = median(trailing_vals[-7:]) if trailing_vals else 0.0

    severity = "info"
    reasons: list[str] = []

    if today_cost > DAILY_COST_CRITICAL_USD:
        severity = "critical"
        reasons.append(f"daily ${today_cost:.2f} > ${DAILY_COST_CRITICAL_USD:.2f}")
    elif today_cost > DAILY_COST_HIGH_USD:
        severity = "high"
        reasons.append(f"daily ${today_cost:.2f} > ${DAILY_COST_HIGH_USD:.2f}")

    if baseline > 0:
        ratio = today_cost / baseline
        if ratio >= DAILY_COST_CRITICAL_MULTIPLIER:
            severity = "critical"
            reasons.append(f"{ratio:.2f}x above 7d median")
        elif ratio >= DAILY_COST_HIGH_MULTIPLIER and SEVERITY_ORDER[severity] < SEVERITY_ORDER["high"]:
            severity = "high"
            reasons.append(f"{ratio:.2f}x above 7d median")
        elif ratio >= DAILY_COST_WARN_MULTIPLIER and SEVERITY_ORDER[severity] < SEVERITY_ORDER["warning"]:
            severity = "warning"
            reasons.append(f"{ratio:.2f}x above 7d median")

    cycle_costs = [
        float(pred.get("total_cost") or 0)
        for pred in data.get("recent_predictions", [])
        if pred.get("total_cost") is not None
    ]
    max_cycle = max(cycle_costs) if cycle_costs else 0.0
    if max_cycle > CYCLE_COST_CRITICAL_USD:
        severity = "critical"
        reasons.append(f"cycle ${max_cycle:.2f} > ${CYCLE_COST_CRITICAL_USD:.2f}")
    elif max_cycle > CYCLE_COST_HIGH_USD and SEVERITY_ORDER[severity] < SEVERITY_ORDER["high"]:
        severity = "high"
        reasons.append(f"cycle ${max_cycle:.2f} > ${CYCLE_COST_HIGH_USD:.2f}")
    elif max_cycle > MAX_COST_PER_CYCLE_USD and SEVERITY_ORDER[severity] < SEVERITY_ORDER["warning"]:
        severity = "warning"
        reasons.append(f"cycle ${max_cycle:.2f} > ${MAX_COST_PER_CYCLE_USD:.2f}")

    if SEVERITY_ORDER[severity] == 0:
        return None

    confidence = "high" if baseline > 0 or max_cycle > 0 else "medium"
    return _make_finding(
        "cost_spike",
        severity,
        confidence,
        ["cost"],
        "; ".join(reasons) if reasons else "Cost anomaly detected",
    )


def should_escalate(finding: dict[str, Any]) -> bool:
    severity = finding["severity"]
    confidence = finding["confidence"]
    risk_tags = set(finding.get("risk_tags", []))
    if severity == "critical":
        return True
    if confidence == "low":
        return True
    if severity == "high" and ("cost" in risk_tags or "functionality" in risk_tags):
        return True
    return False


def build_routine_digest(run_id: str, checks_run: list[str], status_summary: str, deltas: list[str]) -> dict[str, Any]:
    payload = _base_contract(run_id)
    payload.update(
        {
            "type": "routine_digest",
            "checks_run": checks_run,
            "status_summary": status_summary,
            "changes_since_last_run": deltas[:3],
            "action_required": "no",
        }
    )
    return payload


def build_autotriage_report(run_id: str, finding: dict[str, Any]) -> dict[str, Any]:
    payload = _base_contract(run_id)
    payload.update(
        {
            "type": "autotriage_report",
            "incident_id": finding["incident_id"],
            "severity": finding["severity"],
            "confidence": finding["confidence"],
            "risk_tags": finding["risk_tags"],
            "what_was_done": ["Gathered health + api data", f"Classified finding: {finding['check_name']}"],
            "why": finding["detail"],
            "result": "unchanged",
            "rollback_path": "not_needed",
            "action_required": "no",
        }
    )
    return payload


def build_escalation(run_id: str, finding: dict[str, Any]) -> dict[str, Any]:
    payload = _base_contract(run_id)
    payload.update(
        {
            "type": "escalation",
            "incident_id": finding["incident_id"],
            "severity": finding["severity"],
            "confidence": finding["confidence"],
            "risk_tags": finding["risk_tags"],
            "evidence": [finding["detail"]],
            "recommended_safest_action": f"Investigate {finding['check_name']} before applying production changes",
            "alternative_actions": [
                "Increase observation window and re-check in next run (lower risk, slower response)",
                "Apply targeted mitigation immediately (faster, higher regression risk)",
            ],
            "decision_needed_by": (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0).isoformat(),
            "action_required": "yes",
        }
    )
    return payload


def _default_state() -> dict[str, Any]:
    return {"open_incidents": {}}


def load_state() -> dict[str, Any]:
    try:
        with open(STATE_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("open_incidents"), dict):
            return data
    except FileNotFoundError:
        pass
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[state] could not load {STATE_PATH}: {exc}")
    return _default_state()


def save_state(state: dict[str, Any]) -> None:
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=True, indent=2)
        print(f"[state] saved {STATE_PATH} ({len(state.get('open_incidents', {}))} open)")
    except OSError as exc:
        print(f"[state] could not save {STATE_PATH}: {exc}")


def record_open_incident(
    state: dict[str, Any],
    finding: dict[str, Any],
    alert_type: str,
    run_id: str,
) -> None:
    key = finding["check_name"]
    existing = state["open_incidents"].get(key, {})
    state["open_incidents"][key] = {
        "check_name": key,
        "incident_id": finding["incident_id"],
        "severity": finding["severity"],
        "detail": finding["detail"],
        "alert_type": alert_type,
        "opened_at": existing.get("opened_at") or _now_iso(),
        "last_seen_at": _now_iso(),
        "last_run_id": run_id,
    }


def detect_recoveries(
    state: dict[str, Any],
    checks_evaluated: set[str],
    failing_check_names: set[str],
) -> list[dict[str, Any]]:
    """Return open incidents for checks that were evaluated and now pass."""
    recoveries: list[dict[str, Any]] = []
    open_incidents = state.get("open_incidents", {})
    for check_name in list(open_incidents):
        if check_name in checks_evaluated and check_name not in failing_check_names:
            recoveries.append(open_incidents.pop(check_name))
    return recoveries


def build_recovery_report(run_id: str, open_inc: dict[str, Any]) -> dict[str, Any]:
    check_name = open_inc["check_name"]
    payload = _base_contract(run_id)
    payload.update(
        {
            "type": "recovery_report",
            "incident_id": open_inc["incident_id"],
            "check_name": check_name,
            "previous_severity": open_inc["severity"],
            "previous_detail": open_inc["detail"],
            "previous_alert_type": open_inc["alert_type"],
            "verification": f"{check_name} check passed on first clean run",
            "result": "recovered",
            "action_required": "no",
        }
    )
    return payload


def _notify_recoveries(
    run_id: str,
    state: dict[str, Any],
    checks_evaluated: set[str],
    failing_check_names: set[str],
) -> list[dict[str, Any]]:
    recoveries = detect_recoveries(state, checks_evaluated, failing_check_names)
    for open_inc in recoveries:
        notify(build_recovery_report(run_id, open_inc))
        print(f"[recovery] {open_inc['check_name']} cleared")
    return recoveries


def _finalize_run(
    run_id: str,
    state: dict[str, Any],
    findings: list[dict[str, Any]],
    checks_run: list[str],
    checks_evaluated: set[str],
) -> int:
    failing_names = {f["check_name"] for f in findings}
    recoveries = _notify_recoveries(run_id, state, checks_evaluated, failing_names)

    if not findings:
        deltas = ["All checks healthy", "No action needed"]
        if recoveries:
            names = ", ".join(r["check_name"] for r in recoveries)
            deltas = [f"{len(recoveries)} incident(s) recovered: {names}", "All checks healthy"]
        notify(build_routine_digest(run_id, checks_run, "pass", deltas))
        save_state(state)
        return 0

    exit_code = 0
    for finding in findings:
        if should_escalate(finding):
            notify(build_escalation(run_id, finding), immediate=(finding["severity"] == "critical"))
            record_open_incident(state, finding, "escalation", run_id)
            exit_code = 1
        else:
            notify(build_autotriage_report(run_id, finding))
            record_open_incident(state, finding, "autotriage", run_id)

    if exit_code == 0:
        deltas = [f"{len(findings)} non-escalated finding(s) triaged"]
        if recoveries:
            deltas.insert(0, f"{len(recoveries)} incident(s) recovered")
        notify(build_routine_digest(run_id, checks_run, "degraded", deltas))

    save_state(state)
    return exit_code


def _handle_early_failure(
    run_id: str,
    state: dict[str, Any],
    finding: dict[str, Any],
    checks_evaluated: set[str],
) -> int:
    """Record a fatal finding; recover other checks only if evaluated this run."""
    failing = {finding["check_name"]}
    _notify_recoveries(run_id, state, checks_evaluated, failing)
    notify(build_escalation(run_id, finding), immediate=True)
    record_open_incident(state, finding, "escalation", run_id)
    save_state(state)
    return 1


def main() -> int:
    print(f"[healthcheck] base_url={_base_url()}")
    run_id = _run_id()
    state = load_state()
    checks_run = ["/health", "/api/data", "resolver_overdue", "pipeline_stuck", "scan_dead", "cost_spike"]
    findings: list[dict[str, Any]] = []
    checks_evaluated: set[str] = set()

    try:
        with httpx.Client() as client:
            # /health must be strict critical
            checks_evaluated.add("health_endpoint")
            try:
                health = fetch_health(client)
            except Exception as exc:
                finding = _make_finding(
                    "health_endpoint",
                    "critical",
                    "high",
                    ["functionality"],
                    f"/health unreachable: {exc}",
                )
                return _handle_early_failure(run_id, state, finding, checks_evaluated)

            if health.get("status") != "ok":
                finding = _make_finding(
                    "health_endpoint",
                    "critical",
                    "high",
                    ["functionality"],
                    f"/health returned unexpected payload: {health}",
                )
                return _handle_early_failure(run_id, state, finding, checks_evaluated)

            checks_evaluated.add("api_data_endpoint")
            try:
                data = fetch_api_data(client)
            except Exception as exc:
                finding = _make_finding(
                    "api_data_endpoint",
                    "critical",
                    "high",
                    ["functionality"],
                    f"/api/data unavailable: {exc}",
                )
                return _handle_early_failure(run_id, state, finding, checks_evaluated)

            check_specs = [
                (check_resolver, "resolver_overdue"),
                (check_pipeline_stuck, "pipeline_stuck"),
                (check_scan_dead, "scan_dead"),
                (check_cost_spike, "cost_spike"),
            ]
            for check_fn, check_name in check_specs:
                finding = check_fn(data)
                checks_evaluated.add(check_name)
                if finding:
                    findings.append(finding)
                    print(f"[{finding['check_name']}] {finding['severity']} {finding['detail']}")
                else:
                    print(f"[{check_fn.__name__}] ok")

    except Exception as exc:
        checks_evaluated.add("healthcheck_script")
        finding = _make_finding(
            "healthcheck_script",
            "critical",
            "low",
            ["functionality"],
            f"Script crashed unexpectedly: {exc}",
        )
        return _handle_early_failure(run_id, state, finding, checks_evaluated)

    return _finalize_run(run_id, state, findings, checks_run, checks_evaluated)


if __name__ == "__main__":
    sys.exit(main())
