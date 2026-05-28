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

LLOYD_BASE_URL = os.environ["LLOYD_BASE_URL"].strip()

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


def _send_slack(message: str) -> None:
    slack_webhook_url = os.environ.get("SLACK_WEBHOOK_URL", "").strip()
    if not slack_webhook_url:
        print("[notify] slack webhook not configured")
        return
    try:
        resp = httpx.post(slack_webhook_url, json={"text": message}, timeout=10)
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


def notify(payload: dict[str, Any], immediate: bool = False) -> None:
    """Deliver contract payload as compact JSON text."""
    # Prefix helps scanning notifier channels while preserving strict payload.
    prefix = "[IMMEDIATE] " if immediate else ""
    body = json.dumps(payload, ensure_ascii=True)
    message = f"{prefix}{body}"
    _send_slack(message)
    _send_telegram(message)
    print(message)


def _base_contract(run_id: str) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "timestamp_utc": _now_iso(),
        "environment": ENVIRONMENT,
    }


def fetch_health(client: httpx.Client) -> dict[str, Any]:
    resp = client.get(f"{LLOYD_BASE_URL}/health", timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_api_data(client: httpx.Client) -> dict[str, Any]:
    # Retry transient timeouts to reduce noisy critical alerts.
    last_exc: Exception | None = None
    attempts = max(API_DATA_FETCH_ATTEMPTS, 1)
    for attempt in range(1, attempts + 1):
        try:
            resp = client.get(f"{LLOYD_BASE_URL}/api/data", timeout=90)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            last_exc = exc
            if attempt == attempts:
                break
            sleep_s = API_DATA_RETRY_BACKOFF_SECONDS * attempt + random.uniform(0, 0.5)
            print(f"[api_data] attempt {attempt}/{attempts} failed: {exc}; retrying in {sleep_s:.1f}s")
            time.sleep(sleep_s)
    assert last_exc is not None
    raise last_exc


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


def main() -> int:
    run_id = _run_id()
    checks_run = ["/health", "/api/data", "resolver_overdue", "pipeline_stuck", "scan_dead", "cost_spike"]
    findings: list[dict[str, Any]] = []

    try:
        with httpx.Client() as client:
            # /health must be strict critical
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
                notify(build_escalation(run_id, finding), immediate=True)
                return 1

            if health.get("status") != "ok":
                finding = _make_finding(
                    "health_endpoint",
                    "critical",
                    "high",
                    ["functionality"],
                    f"/health returned unexpected payload: {health}",
                )
                notify(build_escalation(run_id, finding), immediate=True)
                return 1

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
                notify(build_escalation(run_id, finding), immediate=True)
                return 1

            for check_fn in (check_resolver, check_pipeline_stuck, check_scan_dead, check_cost_spike):
                finding = check_fn(data)
                if finding:
                    findings.append(finding)
                    print(f"[{finding['check_name']}] {finding['severity']} {finding['detail']}")
                else:
                    print(f"[{check_fn.__name__}] ok")

    except Exception as exc:
        finding = _make_finding(
            "healthcheck_script",
            "critical",
            "low",
            ["functionality"],
            f"Script crashed unexpectedly: {exc}",
        )
        notify(build_escalation(run_id, finding), immediate=True)
        return 1

    if not findings:
        notify(
            build_routine_digest(
                run_id,
                checks_run,
                "pass",
                ["All checks healthy", "No action needed"],
            )
        )
        return 0

    exit_code = 0
    for finding in findings:
        if should_escalate(finding):
            notify(build_escalation(run_id, finding), immediate=(finding["severity"] == "critical"))
            exit_code = 1
        else:
            notify(build_autotriage_report(run_id, finding))

    if exit_code == 0:
        notify(
            build_routine_digest(
                run_id,
                checks_run,
                "degraded",
                [f"{len(findings)} non-escalated finding(s) triaged"],
            )
        )

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
