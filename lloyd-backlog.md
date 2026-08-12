# Lloyd — Product Backlog

**Last updated:** August 8, 2026 (session 2 — Tier 2 root-cause investigation)
**Status:** Living document — update in place as items are completed, de-prioritized, or new ideas surface. Do not save dated copies (e.g. `-0317`, `-0723`) going forward — that pattern caused this doc to fragment across multiple files that drifted out of sync with each other and with the real project state. Edit this file directly.

Items are grouped by area and roughly ordered by priority within each group. The "Trigger" column notes what condition or data signal should prompt picking the item up — most backlog items here are explicitly data-gated, meaning don't build them until you have evidence they're worth building.

> **Consolidation note (Aug 8, 2026):** This file merges `lloyd-backlog-0317.md` (last full copy of the original item set, dated March 17) and `lloyd-backlog-0723.md` (July 23 update, which only recorded deltas and assumed `-0317` would remain available as the base). `-0317` had gone missing from the project folder — it was never committed to git — so this merge restores the full item set into one self-contained file. See the July 23 narrative below for what changed; item tables below reflect the merged, current state.

---

## Recent History

### July 23, 2026 — Root cause found for the "no trades in a week" investigation

Tier 1 had silently degraded to a single model (GPT-4o) for months. Two independent causes, both now fixed:

1. **Gemini was failing on every call** with `429 RESOURCE_EXHAUSTED` — the Google AI Studio account backing `LLOYD_GOOGLE_AI_API_KEY` had run out of prepaid credits. This was NOT a code or SDK problem — `google-genai` (v2.8.0) was already correctly pinned in `uv.lock` and wired into `ensemble.py`; the backlog's old P6 ("Gemini disabled, SDK migration pending") was stale and had already been resolved in code without the doc being updated. Fixed by adding billing credits at ai.studio/projects on 2026-07-23. Confirmed working via a live `uv run` test call against the Gemini API post-fix.
2. **`LLOYD_GPT5_MODEL` was still pinned to `gpt-4o`** from when GPT-5 wasn't yet available/tested (see old P8). GPT-5 has been generally available since August 2025, and OpenAI has since shipped GPT-5.5 (April 2026) and the GPT-5.6 family — Sol/Terra/Luna (June–July 2026). Swapped `LLOYD_GPT5_MODEL` to `gpt-5.6-luna` ($1.00/$6.00 per 1M tokens) — OpenAI's cost-optimized tier, positioned for classification/routing-style tasks, which matches what Tier 1 screening does. This is cheaper on both input and output than the GPT-4o it replaced ($2.50/$10.00 per 1M), so the swap is a net cost reduction, not an increase.
3. **Corrected stale cost tracking** — `gpt5_input_cost_per_1k` / `gpt5_output_cost_per_1k` in `config.py` were set to `0.005`/`0.015`, which never matched actual GPT-4o billing even before the Luna swap. Updated to `0.001`/`0.006` to match Luna's real rates. Cost figures logged between the Luna deploy and this config fix may under/over-report slightly — not retroactively corrected.

**Deployment note:** confirmed the Railway container has been running since a June 18, 2026 deploy. An earlier `ModuleNotFoundError: No module named 'google'` seen when testing via a bare `python3 -c "..."` over `railway ssh` was a shell environment mismatch (not using the `uv`-managed venv), not a real dependency issue. Confirmed via `uv run python3 -c "..."` instead — **use `uv run` for any ad-hoc script over `railway ssh`.**

**Tier 1 is now genuinely two-model again** (GPT-5.6 Luna + Gemini 2.5 Flash), restoring cross-model disagreement detection and the conditions for real Tier 2 (Claude) escalation. Effects not yet observed in resolved trades — too early.

**`LLOYD_RSS_FEEDS` found completely unset in production** — confirmed via `railway ssh` that `len(settings.rss_feeds) == 0`. Since GDELT is hardcoded off (see R3), this meant **every market, every cycle, had zero news context** — `context_quality='none'` unconditionally. Likely a bigger structural contributor to the no-trade problem than the Tier 1 model issues above, since it affects Tier 2 (Claude) as well. Populated with 9 curated, verified-live feeds (BBC world/politics/business/entertainment, Al Jazeera, ESPN, Golf.com, CoinDesk) — see `.env.example`.

**Shipped a real bug in the process** — setting `LLOYD_RSS_FEEDS` as a plain newline-separated string crashed the app on startup in a restart loop (`pydantic-settings` tried its own `json.loads()` before the existing field validator ran). Fixed by annotating the field `Annotated[list[str], NoDecode]`. Latent bug since Stage 2, invisible only because `rss_feeds` had never actually been populated before.

**Known, likely-permanent gap:** MMA prop markets (e.g. regional promotions) have no reliable general news coverage. Not a feed-selection problem — deliberately not chasing a fix.

**Still outstanding:** even with Tier 1 healthy and real news context flowing, the prompt template (`templates.py`, `PROMPT_VERSION=v1.2`) instructs the model to stay within 0.03 of the market price when it lacks a clear edge, and `min_edge_threshold` is also exactly `0.03`. Whether this still structurally suppresses trade signals once real news context is available is not yet known — give it several cycles with feeds live before revisiting. Tracked as P10 below.

### July 23, 2026 (later, same day) — monitoring doc confirmed against source

Four things flagged as unconfirmed hypotheses earlier in the day were checked directly against the actual code:

- Dashboard ⚠ warning icon confirmed as `r.large_move_flagged`, driven by `_price_check_job()` in `main.py` — pure absolute price-move-since-entry (≥10pp), matches original P7 hypothesis.
- `lloyd_healthcheck.py` escalation fields (`alternative_actions`, `decision_needed_by`) confirmed as **hardcoded literals**, not adaptive/LLM-generated — corrected in `lloyd-monitoring-reference.md`.
- Explained why BUY NO signals don't always become open positions: the dashboard renders `ep.trade_signal` (Stage 2 output) with no visibility into whether `RiskSizer.size()` (Stage 3) subsequently blocked the trade. Block reasons logged in `lloyd/risk/sizer.py`: `edge_below_threshold`, `no_model_predictions`, `confidence_below_threshold`, `model_disagreement`, `category_concentration`, `non_positive_kelly`, `exposure_cap`, `zero_quantity`, plus `existing_open_position` (logged separately in `main.py`). New backlog item added for this dashboard gap — see I8.
- Thread closed on the specific Preston Stout/LeBron signal investigation that prompted this session — not worth further digging once the row aged off the dashboard's window.

### August 8, 2026 — Tier 2 (Claude) found completely non-functional since June 15, root-caused and fixed

A go-live check run this session returned NO-GO, with calibration error (21.66%) as the weakest criterion. Pulling the calibration trend from `/api/data`'s `model_scores` (which turns out to already contain daily historical snapshots — no extra tooling needed) showed the overall Brier/calibration/`num_predictions` numbers had been completely frozen since ~July 16, and every row in `model_scores`, going back to March, was labeled `model_name: "gpt-4o"` — no Gemini, Luna, or Claude rows ever appeared.

Traced through the stack step by step:
1. `calibration.py` scores whatever `model_name` values exist in `predictions` — not hardcoded, so the freeze pointed upstream to what's actually being written.
2. A direct query of the `predictions` table (`SELECT model_name, COUNT(*), MIN(created_at), MAX(created_at) ... GROUP BY model_name`) showed `gpt-5.6-luna` and `gemini-2.5-flash` both healthy with predictions through today — the July 23 Tier 1 fix genuinely worked. But `claude-sonnet-4-20250514` showed **zero predictions since 2026-06-15** — 53 days of silence, despite `/api/data`'s `recent_predictions` showing `tier2_used: 1` on markets *today*.
3. `railway logs | grep prediction_failed | grep claude` surfaced the actual error, sitting in plainly-visible `log.error`-level logs the whole time (never `DEBUG`, never filtered by Railway's infra-level log dropping — just never checked): `404 not_found_error — model: claude-sonnet-4-20250514`. Anthropic had retired that dated snapshot.
4. Confirmed current model IDs via `client.models.list()` directly against the live Anthropic key (more reliable than trusting docs/search results) — `claude-sonnet-5` available, cheaper than the old snapshot at introductory pricing ($2/$10 per 1M through 2026-08-31, stepping to $3/$15 after) and benchmarks ahead of Sonnet 4.6.
5. Separately found `ensemble.py` was setting `tier2_used = True` **unconditionally** right after calling `_run_tier2()`, regardless of whether it returned a real result or `None` — this is what let the 404 hide for 53 days across every dashboard, log line, and `model_scores` row that reported on Tier 2 activity.

**Both root causes fixed directly in code (not just Railway env vars — lesson learned from the July 23 GPT-5 swap, which relied on someone remembering to set a dashboard variable):**
- `lloyd/config.py`: `claude_model` default changed from the dead `claude-sonnet-4-20250514` literal to `claude-sonnet-5`; `claude_input_cost_per_1k`/`claude_output_cost_per_1k` updated to `0.002`/`0.010` (intro pricing) with a TODO to revisit after 2026-08-31 when it steps to `0.003`/`0.015`.
- `lloyd/prediction/ensemble.py`: `tier2_used = tier2_result is not None`, replacing the unconditional `True`.

Both changes committed and pushed; Railway auto-deploy picks up the new default with no separate dashboard variable needed. Verification pending — see P11/P12.

**Two more issues found and logged, not yet fixed (both low urgency):**
- `LLOYD_LOG_PATH=/data/lloyd.log` / `config.py`'s `log_path` field do nothing — `structlog` in `main.py` has no file handler configured, logs are stdout-only (which Railway captures fine via `railway logs`). Dead config, not a functional bug. See I9.
- `lloyd.db` has grown to **6.9GB**, plus an unexplained 832MB backup file from April 7. Likely caused by the `markets` table's `UNIQUE(platform, platform_id, fetched_at)` constraint writing a new row (with a full raw JSON blob) on every 30-minute scan rather than updating existing rows. See I10.

**Practical implication for go-live evaluation:** the two-model-Tier-1 + Tier-2-escalation architecture has effectively been **Tier-1-only since June 15** — nearly two months. Every Brier/calibration/go-live number computed in that window reflects a degraded single-tier system, not the intended design. The Aug 8 NO-GO verdict shouldn't be treated as a verdict on the real architecture; give it several weeks of genuine Tier 2 activity post-fix before the next go-live check means anything.

### August 12, 2026 — Kalshi resolver root-caused: wrong hostname, not a Railway DNS outage (corrects Apr 30–May 2 entry below)

A `resolver_overdue` autotriage alert fired on trades 80 and 81 (`KXPAYROLLS-26JUL-T100000`, "Will above 100000 jobs be added in July 2026?"), both `buy_yes`, stuck open 5 days past the Aug 7 close. Confirmed the real-world outcome two independent ways — the BLS jobs report (payrolls fell 23,000 in July) and a direct query against Kalshi's real production API — both say **no**.

**Root cause, corrected from the Apr 30–May 2 entry below:** `kalshi_resolution_base_url` and the hardcoded fallback in `kalshi_resolution.py` both pointed at `https://api.kalshi.com`, believed unreachable from Railway due to DNS. That diagnosis was wrong — **`api.kalshi.com` is not a real Kalshi hostname; it never resolves, from anywhere** (confirmed failing identically from an unrelated sandbox with no Railway-specific restrictions). The May 2 "fix" pointed resolution at the demo API (`demo-api.kalshi.co`) instead, which masked the crash but broke correctness: `fetch_kalshi_market_data()` returns on the *first* HTTP 200 it gets, and the demo host was checked first, so even a fake/never-settling demo response for a matching ticker would win and the function would never reach real market data. The real production host, confirmed reachable and correct, is `https://external-api.kalshi.com` — directly queried and confirmed `KXPAYROLLS-26JUL-T100000` shows `status: finalized, result: no`, matching the BLS data.

**Fixed:**
- `lloyd/config.py`: `kalshi_resolution_base_url` default → `https://external-api.kalshi.com`.
- `lloyd/postmortem/kalshi_resolution.py`: `kalshi_market_request_targets()` reordered so the real production/resolution host is tried *before* the demo trading host (previously demo was checked first, which could shadow prod entirely); hardcoded fallback URL corrected from `api.kalshi.com` to `external-api.kalshi.com`.
- `Dockerfile`: was missing `COPY scripts/ ./scripts/` — `scripts/*.py` (including `settle_trades.py`, the standard manual-remediation tool for exactly this failure mode) has never actually existed inside the deployed container. Discovered when `railway ssh` + `uv run python scripts/settle_trades.py` 404'd on the file. Added.
- Trades 80 and 81 settled manually as `no` via an inline `railway ssh` script (same logic as `settle_trades.py`) pending redeploy: pnl −$118.00 and −$81.84 respectively.

**Verified:** `tests/test_resolver.py`, `test_kalshi_resolution.py`, `test_kalshi.py`, `test_integration.py`, `test_config_port.py` — 38/38 pass (Python 3.11, matching `pyproject.toml`'s `requires-python`). Not yet verified live post-redeploy — next `resolver_overdue` occurrence (or a manual `railway ssh` re-run of `list_overdue_trades.py` against a real stuck market) is the real test.

**Not addressed, flagged for a separate decision:** `kalshi_base_url` (used for market scanning and paper-trade execution/pricing, via `KalshiClient`) still defaults to the demo API. Unlike the resolution URL, this looks like an original architecture choice (paper trading against Kalshi's demo environment) rather than a DNS workaround, so it's out of scope for this fix — but it means every Kalshi trade signal, price, and paper execution has been computed against demo data, not live production prices, for the life of the project. Worth a deliberate look: if demo prices diverge meaningfully from real Kalshi prices, that could affect calibration/go-live numbers in a way similar to the Tier 2 issue above. Not touched here since it's a bigger call than a bug fix.

---

## Prediction & Models

| # | Item | Status | Notes / Trigger |
|---|------|--------|------|
| P6 | ~~Gemini SDK migration + re-enablement~~ | **RESOLVED 2026-07-23** | SDK migration to `google.genai` was already done in code (predates this session). Actual blocker was Google AI Studio prepaid credits being depleted — a billing issue, not code. Fixed by topping up credits. |
| P8 | ~~GPT-5 upgrade~~ | **RESOLVED 2026-07-23** | `LLOYD_GPT5_MODEL` swapped from `gpt-4o` to `gpt-5.6-luna`. Cheaper than GPT-4o on both input/output; chosen over Terra/Sol tiers since Tier 1 is a screening task, not one needing flagship reasoning. |
| P9 | ~~Two-model Tier 1 ensemble~~ | **RESOLVED 2026-07-23** | Direct consequence of P6 — Gemini calls now succeed, so Tier 1 runs GPT-5.6 Luna + Gemini 2.5 Flash concurrently as originally designed. |
| P10 | **Prompt anchoring review** — investigate whether the "stay within 0.03 of market price" instruction in `templates.py` (v1.2), combined with `min_edge_threshold=0.03` and GDELT being disabled (R3), structurally suppresses trade signals even with healthy models | Open — data suggests this is live now that P6/P8/P9 are fixed | Compare trade-signal rate over the next several cycles against historical baseline; if still near-zero despite two working Tier 1 models, this prompt/threshold interaction is the next suspect |
| P11 | **Claude Tier 2 model deprecated — silent total failure, 53 days** | **RESOLVED 2026-08-08, verified live** | `claude-sonnet-4-20250514` was retired by Anthropic; every Tier 2 call since June 15 returned `404 NotFoundError`, caught by the generic `except Exception` in `Predictor.predict()`, logged at `log.error` (visible in `railway logs`, never checked until this session). Fixed in code: `lloyd/config.py` `claude_model` default → `claude-sonnet-5`, cost tracking → `0.002`/`0.010` per 1k (intro pricing through 2026-08-31, revisit after). Committed, pushed, and **confirmed live same day**: `railway logs` showed 4 clean `tier2=True` completions with no errors in the first post-deploy cycle; direct `predictions` table query confirmed 3 real `claude-sonnet-5` rows (first genuine Tier 2 writes in 55 days). |
| P12 | **`tier2_used` flag set unconditionally, independent of P11** | **RESOLVED 2026-08-08, verified live** | `ensemble.py` set `tier2_used = True` immediately after calling `_run_tier2()`, regardless of whether it returned a real result or `None`. This is what let P11 hide for 53 days — dashboard/`model_scores`/go-live check all showed Tier 2 "working" the whole time. Fixed to `tier2_used = tier2_result is not None`. Independent of P11's root cause — would silently mask *any* future Tier 2 failure (rate limit, outage, another deprecation) the same way. Worth a periodic sanity check going forward (e.g. alert if `tier2_used` rate is high but Claude rows in `predictions` aren't growing). |
| P1 | **Brier-weighted ensemble** — replace equal model weights in `_aggregate()` with rolling per-model, per-category Brier scores | Open | Interface already built; weights default to equal until calibration data exists. `model_scores` table has 100+ resolved predictions per model — now achievable for Gemini going forward |
| P2 | **Platt scaling / isotonic regression** — post-hoc recalibration of raw model probabilities | Open | Requires substantial resolved prediction history | 200+ resolved predictions per model; calibration plot shows consistent systematic bias |
| P3 | **Dynamic alpha tuning** — make blend weight adjust based on market liquidity or time to resolution rather than a fixed config value | Open | Current config splits `LLOYD_BUY_YES_ALPHA=0.15` and `LLOYD_MARKET_CONDITIONED_ALPHA=0.30` (buy_no/no_trade) — already more nuanced than the original single `alpha=0.3`. Postmortem shows current split consistently underperforms a smarter rule across 100+ trades |
| P4 | **Base rate database** — pre-populate a lookup table of historical base rates for recurring market categories | Open | High construction effort; LLM already implicitly uses training priors | Prediction reasoning logs show models frequently citing the wrong base rate or stating "no historical data available" for categories with known rates |
| P5 | **Disagreement kill switch tuning** — the Stage 3 risk rule blocks trades where any single model assigns >80% to the opposite direction; threshold may be too tight or too loose | Open | Requires live trading data to evaluate false positive / false negative rate | Kill switch blocking >15% of otherwise-valid trade signals, or post-trade analysis shows blocked signals would have been profitable |
| P7 | **Re-prediction on large price moves** — when the price-check loop flags a 10pp+ move on an open position, trigger a fresh ensemble prediction rather than just logging | Open | Price-check loop is designed to be lightweight; re-prediction adds LLM cost and latency | Postmortem shows flagged positions resolved against the original prediction at a materially higher rate than unflagged ones |

---

## Research & News

| # | Item | Status | Why deferred | Trigger |
|---|------|--------|-------------|---------|
| R3 | **GDELT re-enablement** — re-integrate GDELT web article API with proper rate limiting | **Elevated priority** (Jul 23, 2026) | Disabled entirely due to free tier quota exhaustion. RSS-only was sufficient for early paper trading, but now a suspected contributor to P10 (prompt anchoring suppressing signals) | Elevate to active work if P10 investigation confirms `context_quality='none'`/`'partial'` markets are a large share of `no_trade` outcomes post-Tier-1-fix |
| R1 | **Reddit/asyncpraw integration** — search relevant subreddits for sentiment on market topics, deduplicate against RSS | Open | Adds OAuth setup overhead and subreddit curation work before any calibration data exists to justify it | Postmortem analysis shows >20% of markets had `context_quality='none'` or `'partial'` after 30+ days running (re-evaluate now that RSS feeds are actually populated) |
| R2 | **Category-specific prompt variants** — full separate prompt templates for politics, weather, entertainment, sports | Open | No calibration data yet to know what to tune; premature prompt optimization | Brier scores show a consistent per-category gap (e.g. politics Brier 0.28 vs overall 0.20) across 50+ resolved predictions in that category |
| R4 | **News source weighting** — assign credibility/recency weights to RSS sources | Open | Premature; simple relevance filter is good enough for v1 | Calibration analysis shows systematic bias traceable to low-quality sources |
| R5 | **Longer news lookback window** — extend queries from 7 days to 30 days for slow-moving markets (>60 days to resolution) | Open | RSS feeds provide recent articles only | Stage 4 analysis shows markets >60 days to resolution have materially worse prediction accuracy |
| R6 | **Paid news API** — replace GDELT with a reliable paid source (e.g. NewsAPI, GDELT paid tier, Aylien) | Open | GDELT free tier unreliable; RSS alone may be insufficient for some categories | 30+ days of calibration data shows RSS-only predictions are meaningfully worse; budget allows ~$20-50/month news API spend |

---

## Platform & Execution

| # | Item | Why deferred | Trigger |
|---|------|-------------|---------|
| E1 | **Cross-platform arbitrage trading** — act on the price divergence signal already being logged in `market_pairs` | Resolution divergence risk too high for v1 (same question can resolve differently across platforms) | Postmortem data shows a specific class of cross-platform pairs where resolution divergence has never occurred across 50+ historical examples |
| E2 | **Additional platforms** — Manifold, PredictIt, Metaculus as data sources or trading venues | Out of scope for v1; two platforms is sufficient to validate the approach | Go-live criteria are met on Polymarket + Kalshi; want to expand edge surface area |
| E3 | **Market making / liquidity provision** — post limit orders at the spread rather than taking | Fundamentally different strategy with different risk profile; requires order book depth analysis not built yet | Systematic analysis shows the bot's edge is consistently eroded by crossing the spread |
| E4 | **Live trading ramp tooling** — automated tooling to enforce the 10% → 25% → 50% → 100% capital ramp, gated by paper-vs-live slippage alignment | Not needed until go-live; manual oversight is fine for the ramp | Go-live criteria are met and live trading is enabled |
| E5 | **High-frequency / sub-minute execution** — reduce cycle time below 30 minutes for short-horizon markets | Infrastructure cost, complexity, and regulatory surface area not justified for the target market categories | Evidence that meaningful edge exists in markets resolving within 24 hours that a 30-minute cycle cannot capture |
| E6 | **Partial fill simulation** — simulate partial fills in the paper executor based on order size relative to book depth | Kalshi liquidity field is always 0; pulling live CLOB depth from Polymarket adds API calls to a tight loop; invented partial fill logic would be misleading | Live trading data shows actual fill rates materially below 100% on positions above a certain size |
| E7 | **Dynamic slippage model** — replace flat 0.5% slippage with a model that scales by order size relative to book depth | Flat 0.5% is a reasonable prior; calibrating a better model requires real execution data | Postmortem after going live shows a consistent gap between simulated slippage and actual executed prices |

---

## Infrastructure & Observability

| # | Item | Status | Why deferred / trigger |
|---|------|--------|------|
| I6 | ~~Matcher job decoupling~~ | **RESOLVED March 21, 2026** | Matcher moved to `_matcher_job()`, runs every 6h via thread pool executor, reads latest markets from DB. `run_scan_cycle` unblocked, completes in ~2 min. |
| I4 | **Alerting / paging** — Slack or email alerts for large drawdowns, missed cycles, or unexpected API failures | **Substantially resolved** — GitHub Actions health check + Slack autotriage system deployed April 7, 2026, reworked into a tiered escalation system ~May 29. See `lloyd-monitoring-reference.md`. | Remaining gap: no dedicated large-drawdown check yet (current checks cover resolver overdue, pipeline stuck, scan dead, cost spike) — revisit if a real drawdown event isn't caught |
| I1 | **Postgres migration** — replace SQLite with Postgres on Railway | Open | SQLite WAL mode is sufficient for a single-process monolith | Write contention errors in logs, or query latency on `predictions` table exceeds 100ms consistently |
| I2 | **Mobile-optimised dashboard** — responsive version of the web dashboard for phone monitoring | Open | Current web dashboard built for desktop; functional but not mobile-optimised | Going live and needing to monitor from mobile |
| I3 | **Mobile interface (native app)** | Open — out of scope for v1 | No use case that CLI + web dashboard doesn't cover | — |
| I5 | **Structured cost dashboard** — dedicated view of LLM API spend by model, day, and market category | Open | Structlog output captures cost per prediction; web dashboard shows a 7-day chart; health check now also tracks cost vs. 7-day median (see monitoring reference) | Monthly LLM spend exceeds $40 and you need to identify which categories are most expensive to optimize |
| I7 | **Scanner candidate count tuning** — tighten `LLOYD_MIN_VOLUME` to land in 30–80 range naturally | Open | Currently capped via `LLOYD_MAX_PREDICTION_CANDIDATES`; cap working but scanner doing unnecessary work | After 30 days of data, review whether markets ranked outside the cap ever escalated to Tier 2 or generated a trade signal |
| I8 | **Dashboard: show Stage 3 outcome on the Recent Prediction Signals table** — currently renders only `ep.trade_signal` from Stage 2 (`ensemble_predictions`), with no indication of whether `RiskSizer.size()` subsequently blocked it and why | Open (added 2026-07-23) | Caused real confusion during the July 23 investigation — a row showing `BUY NO` with no corresponding open position looks like a bug but is `RiskSizer` working as designed | Next time the dashboard is touched for any reason — doesn't need its own dedicated session |
| I9 | **`LLOYD_LOG_PATH`/`log_path` config is dead** — nothing writes to `/data/lloyd.log` | Open (found 2026-08-08) | `structlog.configure()` in `main.py` has no file handler; logs go to stdout only (Railway captures this fine via `railway logs`). The `log_path` config field and `LLOYD_LOG_PATH` env var do nothing right now | Low priority since `railway logs` works — either wire up real file logging, or remove the dead config so it stops misleading the next debugging session the way it did this one |
| I10 | **`lloyd.db` has grown to 6.9GB** | Open (found 2026-08-08) | `markets` table stores a full raw JSON API response blob per row (`raw_data TEXT`), and `UNIQUE(platform, platform_id, fetched_at)` means every 30-minute scan writes new rows rather than updating existing ones — unbounded growth since March 21. Also an unexplained 832MB stale backup (`lloyd.db.bak.20260407182232`) with no clear retention policy | Moderate — not causing errors yet, but a real risk to Railway volume costs/disk limits on current trajectory. Ties into I1 (Postgres migration) from a different angle. Decide: (a) whether `raw_data` needs per-scan or just per-change retention, (b) a pruning policy, (c) whether the April 7 backup is safe to delete |

---

## Post-Go-Live

| # | Item | Why deferred | Trigger |
|---|------|-------------|---------|
| G1 | **Automatic ensemble weight updates** — `PostmortemAnalyzer` is spec'd to update weights; wire this into the live pipeline | Can't tune weights until there are resolved predictions to tune against | 100+ resolved predictions per model in `model_scores` table |
| G2 | **Platt scaling pipeline** — fit calibration curves per model using resolved outcomes, apply before ensemble aggregation | Requires a recalibration training set that doesn't exist yet | See P2 trigger above |
| G3 | **Capital allocation by category** — instead of a single bankroll, allocate separate Kelly fractions per category | Adds complexity before there's any evidence that per-category allocation improves returns | Postmortem shows Sharpe ratio would materially improve if weather/entertainment got higher allocation than politics/finance |
| G4 | **Automated go-live re-evaluation** — run `go_live_check.py` on a schedule and send a notification when criteria flip to YES | Manual check is fine during paper trading | Paper trading phase exceeds 60 days and you want automated monitoring rather than checking manually |

---

## Won't Do (explicit non-goals)

Items that are permanently out of scope unless the project's purpose fundamentally changes:

- **Sub-second / high-frequency trading** — requires co-location, exchange connectivity agreements, and an entirely different risk model
- **Market making as primary strategy** — different business model; Lloyd's edge is in prediction accuracy, not spread capture
- **Automated regulatory compliance tooling** — CFTC/SEC compliance for prediction markets is a legal question, not a software question; consult a lawyer before going live
- **Multi-user / SaaS version** — solo dev project; productizing it adds auth, billing, and customer support overhead with no clear upside

---

## How to use this document

When picking up a backlog item:
1. Check the trigger condition — if it hasn't been met, push back
2. Add a decisions log entry in `lloyd-handoff.md` explaining why you picked it up now
3. Mark the item RESOLVED with a date and a one-line note (don't delete it — keep the history), or move it to an "In Progress" section at the top
4. If you discover a new deferred item during a build session, add it here before closing the conversation
5. **Edit this file in place.** Don't save a new dated copy — that's how this doc ended up fragmented across three files that drifted out of sync.
