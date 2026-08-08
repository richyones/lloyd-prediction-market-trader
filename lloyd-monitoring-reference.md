# Lloyd Monitoring Reference

**Last updated:** July 23, 2026 (confirmed against live `lloyd_healthcheck.py` source, later same day)

## Overview

Lloyd uses a lightweight external monitoring layer via **GitHub Actions** — a Python health check script that runs every 6 hours and sends Slack alerts if anything is wrong. There is no additional infrastructure; it runs against the existing `/health` and `/api/data` endpoints on Railway.

Railway Pro deployment notifications are also enabled separately for container crash/restart alerts.

**Note on this update:** earlier versions of this doc described a simple pass/fail alert format. Around May 29, 2026, the script was reworked into a three-tier (actually four, see below) **autotriage** system. That redesign was never folded back into this doc at the time — this version was first reconstructed from old chat history, then **confirmed directly against the actual `lloyd_healthcheck.py` source** the same day. Two things the reconstruction got wrong are corrected below.

---

## Files

| File | Location |
|---|---|
| Health check script | `lloyd_healthcheck.py` (repo root) |
| GitHub Actions workflow | `.github/workflows/lloyd-healthcheck.yml` |

---

## What It Checks

The script hits `/health` (liveness) and `/api/data` (data checks) on the Railway deployment and runs 4 substantive checks, plus the two endpoint-reachability checks that fail fast:

| # | Check | How It Works | Threshold |
|---|---|---|---|
| — | **`/health` reachability** | Must return `{"status": "ok"}` | Any failure → immediate critical escalation, script exits early |
| — | **`/api/data` reachability** | Must return valid JSON | Any failure → immediate critical escalation, script exits early |
| 1 | **`resolver_overdue`** | Open trades where `close_date` is past by >N days but trade still open | `RESOLVER_LOOKBACK_DAYS=3`. Severity `high` if count > `RESOLVER_HIGH_COUNT=5`, else `warning` |
| 2 | **`pipeline_stuck`** | Last entry in `recent_predictions` older than a threshold | **Dynamic**, not fixed — see below |
| 3 | **`scan_dead`** | Same staleness signal, longer window | **Dynamic**, not fixed — see below |
| 4 | **`cost_spike`** | Per-cycle cost, daily cost, and daily cost vs. 7-day median, each against their own thresholds | See cost table below |

All thresholds are configurable via GitHub Actions env vars (also mirrored in `.env.example` for local reference) — no code change needed.

**Correction — pipeline/scan thresholds are dynamic, not the flat `PIPELINE_STUCK_HOURS=4`/`SCAN_DEAD_HOURS=6` env defaults:**
```python
# pipeline_stuck threshold:
interval + PIPELINE_STUCK_GRACE_HOURS   # if prediction_interval_hours is known from /api/data
# else falls back to PIPELINE_STUCK_HOURS

# scan_dead threshold:
max(SCAN_DEAD_HOURS, interval * 2)
```
With `prediction_interval_hours=6` (current setting) and `PIPELINE_STUCK_GRACE_HOURS=1` (default), the **actual effective thresholds right now are ~7h for pipeline_stuck and ~12h for scan_dead** — looser than the raw env var values suggest. Severity within each check escalates from `high` to `critical` if staleness exceeds 2× the threshold.

---

## Cost Thresholds

| Metric | Warn | High | Critical |
|---|---|---|---|
| Cost per cycle | >$2.00 (`MAX_COST_PER_CYCLE_USD`) | >$3.00 (`CYCLE_COST_HIGH_USD`) | >$5.00 (`CYCLE_COST_CRITICAL_USD`) |
| Daily spend | — | >$20.00 (`DAILY_COST_HIGH_USD`) | >$35.00 (`DAILY_COST_CRITICAL_USD`) |
| vs. 7-day median | 1.25× (`DAILY_COST_WARN_MULTIPLIER`) | 1.5× (`DAILY_COST_HIGH_MULTIPLIER`) | 2.0× (`DAILY_COST_CRITICAL_MULTIPLIER`) |

All three metrics are evaluated independently in `check_cost_spike()`; the finding takes the **highest** severity triggered across all of them. A finding at **high** severity with the `cost` risk tag → escalation (cost findings always carry the `cost` tag). **Critical always escalates**, regardless of tag.

**Confirmed live 2026-07-23:** an alert fired reporting "2.34x above 7d median" — correctly exceeds the 2.0× critical threshold on the vs-median metric, matching `check_cost_spike()` exactly (`ratio = today_cost / median(trailing 7 days)`). Likely cause: same-day changes to re-enable Gemini (real billed calls instead of instant billing-error failures), populate `LLOYD_RSS_FEEDS` (real article text now included in every prompt, increasing input tokens), and restored Tier 2 (Claude) escalations now that genuine cross-model disagreement exists. Actual daily dollar figure from `/api/data` → `cost_by_day` still worth a look to confirm it's a modest, explainable increase rather than something more extreme (e.g., a malformed RSS feed bloating a prompt) — not yet done as of this writing.

---

## Schedule

- **Every 6 hours** — automated checks (cron: `0 */6 * * *` UTC)
- **Manual trigger** — available from GitHub Actions UI (`workflow_dispatch`)

---

## Notifications

Alerts go to **Slack** via webhook. Telegram is also supported (same content, plain-text formatted instead of Block Kit).

**GitHub Secrets / env vars required:**

| Secret | Required | Notes |
|---|---|---|
| `LLOYD_BASE_URL` | Yes | Railway public domain only — no path suffix (script auto-strips `/health` or `/api/data` if pasted by accident) |
| `SLACK_WEBHOOK_URL` | Yes | Incoming webhook |
| `TELEGRAM_BOT_TOKEN` | No | Same content sent to Telegram if both token and chat ID are set |
| `TELEGRAM_CHAT_ID` | No | Pair with bot token |

---

## Alert Format — JSON Contracts

Slack messages use real Block Kit (`blocks` array + a plain-text `fallback`), not raw JSON dumped into the `text` field as earlier docs assumed. **Four** message types exist — the fourth (`recovery_report`) wasn't documented anywhere before this update:

### `routine_digest`
Sent on every run that ends clean or with only non-escalated findings.
```json
{
  "type": "routine_digest",
  "run_id": "run_20260723T140000_abcd1234",
  "timestamp_utc": "2026-07-23T14:00:00+00:00",
  "environment": "production",
  "checks_run": ["/health", "/api/data", "resolver_overdue", "pipeline_stuck", "scan_dead", "cost_spike"],
  "status_summary": "pass",
  "changes_since_last_run": ["All checks healthy", "No action needed"],
  "action_required": "no"
}
```
`status_summary` is `"pass"` (fully clean) or `"degraded"` (non-escalated findings present).

### `autotriage_report`
Sent for a finding that doesn't meet the escalation bar.
```json
{
  "type": "autotriage_report",
  "incident_id": "a1b2c3d4e5f6",
  "severity": "high",
  "confidence": "high",
  "risk_tags": ["functionality"],
  "what_was_done": ["Gathered health + api data", "Classified finding: resolver_overdue"],
  "why": "2 overdue open trade(s) beyond 3d: kalshi:...",
  "result": "unchanged",
  "action_required": "no"
}
```

### `escalation`
Sent when `should_escalate()` returns true (see logic below). Critical severity is additionally sent with `immediate=True`, which prefixes the Slack title with `🚨 IMMEDIATE —`.
```json
{
  "type": "escalation",
  "incident_id": "a1b2c3d4e5f6",
  "severity": "critical",
  "confidence": "high",
  "risk_tags": ["cost"],
  "evidence": ["2.34x above 7d median"],
  "recommended_safest_action": "Investigate cost_spike before applying production changes",
  "alternative_actions": [
    "Increase observation window and re-check in next run (lower risk, slower response)",
    "Apply targeted mitigation immediately (faster, higher regression risk)"
  ],
  "decision_needed_by": "2026-07-23T16:00:00+00:00",
  "action_required": "yes"
}
```

**Corrected — `alternative_actions` and `decision_needed_by` are NOT adaptive/LLM-generated.** An earlier version of this doc guessed they might be, since the language reads like generated reasoning ("lower risk, slower response" vs. "faster, higher regression risk"). Confirmed false against `build_escalation()` in the actual source: both are **hardcoded literals**, identical on every single escalation regardless of which check triggered it or what the finding actually is:
```python
"alternative_actions": [
    "Increase observation window and re-check in next run (lower risk, slower response)",
    "Apply targeted mitigation immediately (faster, higher regression risk)",
],
"decision_needed_by": (datetime.now(timezone.utc) + timedelta(hours=2)).replace(microsecond=0).isoformat(),
```
`recommended_safest_action` is also a fixed template — `f"Investigate {check_name} before applying production changes"` — just with the check name interpolated in. Despite the "autotriage" name, there is no LLM in this script at all; it's a deterministic rules engine with a nice Slack renderer on top.

### `recovery_report` (previously undocumented)
Sent automatically the first time a previously-failing check passes again, so you know something self-resolved without having to notice the absence of an alert.
```json
{
  "type": "recovery_report",
  "check_name": "cost_spike",
  "previous_severity": "critical",
  "previous_detail": "2.34x above 7d median",
  "previous_alert_type": "escalation",
  "verification": "cost_spike check passed on first clean run",
  "result": "recovered",
  "action_required": "no"
}
```

---

## Escalation Logic

`should_escalate(finding)` is true when any of:
- `severity == "critical"`
- `confidence == "low"` (uncertain finding — safer to page than to miss it)
- `severity == "high"` AND `risk_tags` includes `"cost"` or `"functionality"`

Everything else → `autotriage_report`. No finding at all across all checks → `routine_digest`.

---

## State & Deduplication (previously undocumented)

The script persists state to `.healthcheck-state.json` (path configurable via `HEALTHCHECK_STATE_PATH`) between runs, tracking currently-open incidents per `check_name`. Two behaviors depend on this:
- **No duplicate escalation spam** — if a check is still failing on the next run with an already-open escalation, `should_notify_escalation()` suppresses a repeat Slack message (still logged to stdout, just not re-sent).
- **Automatic recovery detection** — `detect_recoveries()` compares this run's evaluated-but-passing checks against previously-open incidents and fires a `recovery_report` for anything that cleared.

Worth knowing this file exists on whatever runner executes the GitHub Action — if using ephemeral runners, state won't persist between runs and every failing check will re-escalate every 6 hours indefinitely. (Not yet confirmed whether the current setup uses persistent or ephemeral runners — worth checking.)

---

## Common Mistakes

- **`LLOYD_BASE_URL` includes `/health` or `/api/data`** — script strips these suffixes automatically, but domain should be clean regardless.
- **No Slack message** — missing `SLACK_WEBHOOK_URL` (script logs `[notify] slack webhook not configured` and continues).
- **False pipeline alerts** — remember thresholds are dynamic based on `prediction_interval_hours` from `/api/data`, not the raw env var value alone (see correction above). If prediction frequency changes, the effective threshold changes automatically — no env var update needed for that specific case.

---

## Adding New Checks

1. Add a `check_*` function in `lloyd_healthcheck.py` returning `_make_finding(...)` or `None`, following the existing pattern
2. Add it to the `check_specs` list in `main()`
3. No workflow changes needed unless introducing a new env-var threshold

---

## Known Limitations

- **6h detection latency** — worst case, an issue goes undetected for up to 6 hours
- **No log-level visibility** — only sees `/api/data` output, not raw Railway logs. Issues that don't surface there (e.g. DNS errors not affecting settlement state) won't trigger alerts
- **Resolver check is trade-state-based** — detects overdue unsettled trades, not resolver attempt failures. A resolver failing silently with no due trades won't fire
- **`recent_predictions` as scan/pipeline proxy** — if the scan runs but produces zero candidates (all filtered out), no alert fires — there's no distinct "zero candidates" check
- **State file persistence assumption** — see State & Deduplication above; unconfirmed whether the GitHub Actions runner setup actually persists it between runs

---

## History

- **April 7, 2026** — Initial deployment, pulled forward after a resolver silent-failure bug caused undetected losses over ~2 weeks.
- **April 30, 2026** — Correctly flagged resolver silent failure on trades 13, 14, 16 (DNS error against `api.kalshi.com`).
- **May 2, 2026** — Root cause fixed (`kalshi_resolution_base_url` config). `RESOLVER_LOOKBACK_DAYS` bumped to 10 temporarily.
- **~May 29, 2026** — Reworked into the autotriage system described above. Thresholds tightened (`RESOLVER_LOOKBACK_DAYS` 10→3, `PIPELINE_STUCK_HOURS` 8→4, `SCAN_DEAD_HOURS` 10→6 — noting these are now dynamic floors, not hard thresholds). Cost check expanded to the three-metric table. **This doc wasn't updated at the time.**
- **July 23, 2026 (morning)** — First confirmed critical cost-spike escalation under the new system (2.34× 7-day median), coinciding with same-day Gemini/RSS/model-swap changes. Doc reconstructed from old chat history since local file access wasn't available at the time (mobile session) — two fields (`alternative_actions`, `decision_needed_by`) flagged as unconfirmed guesses.
- **July 23, 2026 (later same day)** — Doc corrected against actual `lloyd_healthcheck.py` source now that filesystem access was available. `alternative_actions`/`decision_needed_by` confirmed as fixed templates, not adaptive. Dynamic pipeline/scan thresholds, `recovery_report` message type, and state/dedup behavior documented for the first time. Also confirmed (in `main.py`/`dashboard.html`, not this script) the dashboard's ⚠ open-position icon is `large_move_flagged`, driven by `_price_check_job()` — unrelated to this health check but was bundled into the same verification pass.
