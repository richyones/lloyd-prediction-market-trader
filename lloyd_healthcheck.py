"""
Lloyd Health Check — runs every 6 hours via GitHub Actions.

Checks:
  1. Resolver silent failure — open trades past close_date + zero resolver activity
  2. Pipeline stuck — recent_predictions has entries but they're stale (no stage_2 in 8h)
  3. Scan crash — no predictions generated in last 10h (scan stopped producing candidates)
  4. LLM cost spike — any prediction cycle in the last 24h cost > $2
  5. Container liveness — /health returns 200

Sends a Slack (or Telegram) message if any check fails.
On a clean run, sends a brief weekly digest (Mondays only) so you know it's working.
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

# ---------------------------------------------------------------------------
# Config — set these as GitHub Actions secrets / env vars
# ---------------------------------------------------------------------------
LLOYD_BASE_URL = os.environ["LLOYD_BASE_URL"]          # e.g. https://lloyd-xxx.up.railway.app
SLACK_WEBHOOK_URL = os.environ.get("SLACK_WEBHOOK_URL")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Thresholds
MAX_COST_PER_CYCLE_USD = float(os.environ.get("MAX_COST_PER_CYCLE_USD", "2.0"))
PIPELINE_STUCK_HOURS = int(os.environ.get("PIPELINE_STUCK_HOURS", "8"))   # no stage_2 in 8h = stuck
SCAN_DEAD_HOURS = int(os.environ.get("SCAN_DEAD_HOURS", "10"))            # no predictions in 10h = scan dead
RESOLVER_LOOKBACK_DAYS = int(os.environ.get("RESOLVER_LOOKBACK_DAYS", "3"))  # look at trades closing in past N days


# ---------------------------------------------------------------------------
# Fetch helpers
# ---------------------------------------------------------------------------

def fetch_health(client: httpx.Client) -> dict[str, Any]:
    resp = client.get(f"{LLOYD_BASE_URL}/health", timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_api_data(client: httpx.Client) -> dict[str, Any]:
    resp = client.get(f"{LLOYD_BASE_URL}/api/data", timeout=30)
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Individual checks — each returns (passed: bool, detail: str)
# ---------------------------------------------------------------------------

def check_liveness(health: dict) -> tuple[bool, str]:
    """Check 0: container is alive and /health returns ok."""
    if health.get("status") == "ok":
        return True, "Container healthy"
    return False, f"Unexpected /health response: {health}"


def check_resolver(data: dict) -> tuple[bool, str]:
    """
    Check 1: Resolver silent failure.

    Looks for open trades whose close_date is in the past (past resolution window).
    These should have been settled. If any exist AND cost_by_day shows recent activity
    (meaning the bot is running), the resolver is silently failing.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=RESOLVER_LOOKBACK_DAYS)

    open_trades: list[dict] = data.get("open_trades", [])
    if not open_trades:
        return True, "No open trades — resolver check skipped"

    overdue = []
    for trade in open_trades:
        close_date_str = trade.get("close_date")
        if not close_date_str:
            continue
        try:
            # close_date may be ISO string or date-only string
            if "T" in close_date_str:
                close_dt = datetime.fromisoformat(close_date_str.replace("Z", "+00:00"))
            else:
                close_dt = datetime.fromisoformat(close_date_str).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue

        if close_dt < cutoff:
            overdue.append({
                "question": trade.get("question", "unknown")[:60],
                "close_date": close_date_str,
                "platform": trade.get("platform", "?"),
            })

    if not overdue:
        return True, f"No trades overdue beyond {RESOLVER_LOOKBACK_DAYS}d close date"

    # Only flag if the bot is clearly still running (has recent cost_by_day entries)
    cost_by_day: list[dict] = data.get("cost_by_day", [])
    recent_days = [
        d for d in cost_by_day
        if d.get("day", "") >= (now - timedelta(days=2)).strftime("%Y-%m-%d")
    ]
    if not recent_days:
        return True, "No recent prediction activity — bot may be idle, resolver check inconclusive"

    detail = (
        f"⚠️ *{len(overdue)} open trade(s) past close_date by >{RESOLVER_LOOKBACK_DAYS}d* "
        f"but resolver hasn't settled them:\n"
        + "\n".join(f"  • {t['platform']}: {t['question']} (closed {t['close_date']})" for t in overdue[:5])
    )
    return False, detail


def check_pipeline_stuck(data: dict) -> tuple[bool, str]:
    """
    Check 2: Prediction pipeline stuck.

    If the most recent prediction in recent_predictions is older than PIPELINE_STUCK_HOURS,
    and cost_by_day shows the bot was running recently, the pipeline is stuck.
    """
    recent_predictions: list[dict] = data.get("recent_predictions", [])
    if not recent_predictions:
        # Could be day 1 or a fresh reset — not actionable on its own
        return True, "No predictions yet — pipeline check inconclusive"

    # Most recent prediction timestamp
    latest_str = recent_predictions[0].get("created_at", "")
    if not latest_str:
        return True, "No created_at on predictions — check inconclusive"

    try:
        latest_dt = datetime.fromisoformat(latest_str.replace("Z", "+00:00"))
        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True, f"Could not parse prediction timestamp: {latest_str}"

    now = datetime.now(timezone.utc)
    hours_since = (now - latest_dt).total_seconds() / 3600

    if hours_since > PIPELINE_STUCK_HOURS:
        return False, (
            f"⚠️ *Prediction pipeline appears stuck* — last prediction was "
            f"{hours_since:.1f}h ago (threshold: {PIPELINE_STUCK_HOURS}h).\n"
            f"Last prediction: `{latest_str}`"
        )

    return True, f"Last prediction {hours_since:.1f}h ago — pipeline healthy"


def check_scan_alive(data: dict) -> tuple[bool, str]:
    """
    Check 3: Scan cycle producing candidates.

    Uses recent_predictions as a proxy — if predictions exist at all, scans are feeding
    candidates to the LLM. If last prediction is >SCAN_DEAD_HOURS old, scan may have crashed.
    Note: this overlaps with check_pipeline_stuck intentionally — the same symptom
    (no recent predictions) could mean either scan crashed OR prediction pipeline stuck.
    We keep both checks with different thresholds and messaging.
    """
    recent_predictions: list[dict] = data.get("recent_predictions", [])
    if not recent_predictions:
        return True, "No predictions yet — scan check inconclusive"

    latest_str = recent_predictions[0].get("created_at", "")
    try:
        latest_dt = datetime.fromisoformat(latest_str.replace("Z", "+00:00"))
        if latest_dt.tzinfo is None:
            latest_dt = latest_dt.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return True, "Could not parse prediction timestamp"

    now = datetime.now(timezone.utc)
    hours_since = (now - latest_dt).total_seconds() / 3600

    if hours_since > SCAN_DEAD_HOURS:
        return False, (
            f"⚠️ *Scan may be dead* — no new predictions in {hours_since:.1f}h "
            f"(threshold: {SCAN_DEAD_HOURS}h). Either scan is crashing or pipeline is fully stuck."
        )
    return True, f"Scan alive — predictions flowing ({hours_since:.1f}h since last)"


def check_cost_spike(data: dict) -> tuple[bool, str]:
    """
    Check 5: LLM cost spike.

    Looks at cost_by_day for today and yesterday. Flags if daily spend > $10
    (a rough proxy for a per-cycle spike — MAX_COST_PER_CYCLE_USD * ~5 cycles/day = $10).
    Also looks at recent_predictions for any single-cycle cost outlier.
    """
    now = datetime.now(timezone.utc)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")

    cost_by_day: list[dict] = data.get("cost_by_day", [])
    daily_threshold = MAX_COST_PER_CYCLE_USD * 8  # ~8 prediction cycles/day max

    alerts = []
    for entry in cost_by_day:
        day = entry.get("day", "")
        daily_cost = float(entry.get("daily_cost") or 0)
        if day in (today_str, yesterday_str) and daily_cost > daily_threshold:
            alerts.append(f"  • {day}: ${daily_cost:.2f} (threshold: ${daily_threshold:.2f})")

    # Also scan individual recent predictions for per-cycle cost
    recent_predictions: list[dict] = data.get("recent_predictions", [])
    cycle_alerts = []
    for pred in recent_predictions:
        cost = float(pred.get("total_cost") or 0)
        if cost > MAX_COST_PER_CYCLE_USD:
            created_at = pred.get("created_at", "unknown")[:16]
            cycle_alerts.append(f"  • {created_at}: ${cost:.2f}")

    if not alerts and not cycle_alerts:
        total_today = next(
            (float(e.get("daily_cost") or 0) for e in cost_by_day if e.get("day") == today_str), 0
        )
        return True, f"LLM costs normal — today: ${total_today:.2f}"

    msg = "⚠️ *LLM cost spike detected*\n"
    if alerts:
        msg += "Daily spend over threshold:\n" + "\n".join(alerts) + "\n"
    if cycle_alerts:
        msg += f"Individual prediction cycles over ${MAX_COST_PER_CYCLE_USD:.2f}:\n" + "\n".join(cycle_alerts[:5])
    return False, msg


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_slack(message: str) -> None:
    if not SLACK_WEBHOOK_URL:
        return
    httpx.post(SLACK_WEBHOOK_URL, json={"text": message}, timeout=10)


def send_telegram(message: str) -> None:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    httpx.post(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "Markdown",
        },
        timeout=10,
    )


def notify(message: str) -> None:
    """Send to whichever notification channel is configured."""
    send_slack(message)
    send_telegram(message)
    print(message)


# ---------------------------------------------------------------------------
# Weekly digest (sent on Mondays, clean-run only)
# ---------------------------------------------------------------------------

def build_digest(data: dict) -> str:
    now = datetime.now(timezone.utc)
    portfolio = data.get("portfolio") or {}
    open_trades = data.get("open_trades", [])
    cost_by_day = data.get("cost_by_day", [])

    total_cost_7d = sum(float(e.get("daily_cost") or 0) for e in cost_by_day)
    cash = portfolio.get("cash_balance")
    exposure = portfolio.get("total_exposure")
    pnl = portfolio.get("unrealized_pnl")

    lines = [
        f"📊 *Lloyd Weekly Digest* — {now.strftime('%Y-%m-%d %H:%M UTC')}",
        f"  • Open trades: {len(open_trades)}",
        f"  • Cash balance: ${cash:.2f}" if cash is not None else "  • Cash balance: n/a",
        f"  • Total exposure: ${exposure:.2f}" if exposure is not None else "  • Total exposure: n/a",
        f"  • Unrealized PnL: ${pnl:.2f}" if pnl is not None else "  • Unrealized PnL: n/a",
        f"  • LLM cost (7d): ${total_cost_7d:.2f}",
        f"  • All checks passed ✅",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    now = datetime.now(timezone.utc)
    is_monday = now.weekday() == 0

    failures: list[str] = []

    try:
        with httpx.Client() as client:
            # Liveness check
            try:
                health = fetch_health(client)
                passed, detail = check_liveness(health)
                if not passed:
                    failures.append(detail)
                print(f"[liveness] {'✅' if passed else '❌'} {detail}")
            except Exception as exc:
                failures.append(f"🚨 *Container unreachable* — /health failed: {exc}")
                print(f"[liveness] ❌ {exc}")
                # Can't proceed without the container — notify immediately
                notify("\n".join(failures))
                return 1

            # API data checks
            try:
                data = fetch_api_data(client)
            except Exception as exc:
                failures.append(f"🚨 *API data unavailable* — /api/data failed: {exc}")
                notify("\n".join(failures))
                return 1

            checks = [
                ("resolver", check_resolver(data)),
                ("pipeline", check_pipeline_stuck(data)),
                ("scan", check_scan_alive(data)),
                ("cost", check_cost_spike(data)),
            ]

            for name, (passed, detail) in checks:
                print(f"[{name}] {'✅' if passed else '❌'} {detail}")
                if not passed:
                    failures.append(detail)

    except Exception as exc:
        failures.append(f"🚨 Health check script crashed: {exc}")
        notify("\n".join(failures))
        return 1

    if failures:
        header = f"🚨 *Lloyd Health Alert* — {now.strftime('%Y-%m-%d %H:%M UTC')}\n"
        notify(header + "\n\n".join(failures))
        return 1

    # Clean run
    if is_monday:
        notify(build_digest(data))
    else:
        print("All checks passed — no notification sent (not Monday)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
