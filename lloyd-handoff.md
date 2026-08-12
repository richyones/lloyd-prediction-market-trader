# Lloyd Project — Conversation Handoff / Operations Runbook

**Last updated:** August 8, 2026 (session 2)

**Instructions:** Copy the PROJECT CONTEXT and CURRENT STATUS sections below into a new conversation when starting a fresh session on Lloyd. Update CURRENT STATUS and DECISIONS LOG each time you complete work. **Edit this file in place — do not save a dated copy.** This file replaces the `lloyd-handoff-stage5.md` / `5.2` / `5_3` / `5_4` series, which fragmented across four files and then went missing from the project folder entirely (never committed to git). This version was reconstructed from the last surviving copy (`stage5_4`, March 21, 2026) plus git history, `.specstory` session logs, and `lloyd-monitoring-reference.md` / `lloyd-backlog.md`, so the gap between March 21 and July 23 is summarized rather than logged turn-by-turn.

---

## PROJECT CONTEXT

I'm building **Lloyd**, an AI-powered prediction market trading bot that trades on Polymarket (on-chain, Polygon/USDC) and Kalshi (CFTC-regulated US exchange). Solo developer, Python, deploying on Railway.

**Project path:** `/Users/richyones/Library/Mobile Documents/com~apple~CloudDocs/Docs New/Projects/Lloyd`

**Architecture decisions (locked in):**
- Plain Python monolith — no multi-agent frameworks (CrewAI, LangGraph, etc.)
- LLMs called as stateless functions, not agents
- Tiered LLM ensemble: **GPT-5.6 Luna + Gemini 2.5 Flash for Tier 1 screening, Claude Sonnet 5 for Tier 2 deep analysis** on high-edge markets (Tier 1 updated 2026-07-23; Tier 2 model fixed 2026-08-08 after being silently dead since June 15 — see Decisions Log)
- SQLite with WAL mode (migrate to Postgres only if needed — see backlog I1)
- Quarter-Kelly position sizing, max 5% per position, max 20% total exposure
- Paper trading first, live trading only after meeting strict go-live criteria
- Target markets: weather, entertainment, world events (documented 2-7pp exploitable edge)

**Build stages:**
1. ✅ Foundation — API integrations, market scanner, cross-platform matcher
2. ✅ Research & prediction — RSS news retrieval, multi-model LLM ensemble, calibration
3. ✅ Risk & paper trading — Kelly sizing, simulated execution with realistic slippage/fees
4. ✅ Postmortem & evaluation — Brier score tracking, calibration analysis, go-live check
5. 🔄 Railway deployment + paper trading evaluation period → live trading activation (still the current stage as of Aug 8, 2026)

**Key files:**
- `lloyd-prd.md` — Full PRD with schemas, acceptance criteria, config specs
- `lloyd-backlog.md` — Deferred items with data-gated trigger conditions (single living file as of Aug 8, 2026)
- `lloyd-handoff.md` — This file
- `lloyd-monitoring-reference.md` — GitHub Actions health-check / autotriage system reference (separate from this file — covers the alerting layer specifically)
- `.env.example` — **Source of truth for current config values.** Don't rely on env var snapshots in old handoff versions; they go stale fast. Check `.env.example` (git-tracked) or `railway ssh` + `uv run` for what's actually deployed.

**Tech stack:** Python 3.11+, uv, httpx, pydantic-settings, structlog, rapidfuzz, APScheduler, anthropic, openai, google-genai, feedparser, pytest, rich (CLI dashboard)

**Operational conventions (per project instructions):**
- To query the live database, use the `/api/data` endpoint rather than connecting to SQLite directly.
- To run scripts against the live deployment, use `railway ssh` — and run them via `uv run` (a bare `python3 -c "..."` over `railway ssh` doesn't use the project's managed venv and will fail on real dependencies like `google-genai` with a misleading `ModuleNotFoundError`).
- When a feature is skipped or removed to simplify operations, update `lloyd-backlog.md` to record the deviation from the original plan.

---

## CURRENT STATUS

**Stage:** 5 — Railway deployment & paper trading evaluation period

**Last confirmed session:** August 8, 2026, session 2 (see Decisions Log for details). A go-live check returned NO-GO; digging into the weakest criterion (calibration error) surfaced that **Tier 2 (Claude) had been completely non-functional since June 15** — a deprecated model snapshot (`claude-sonnet-4-20250514`) 404'd on every call for 53 days, invisible because a separate bookkeeping bug (`tier2_used` set unconditionally) made every dashboard/log/model_scores view report Tier 2 as working. Both root causes fixed in code and pushed: model swapped to `claude-sonnet-5`, `tier2_used` now reflects the real result. **Verified live same day** — `railway logs` showed 4 clean `tier2=True` completions with no errors in the first post-deploy cycle, and a direct `predictions` table query confirmed 3 real `claude-sonnet-5` rows landed (first genuine Tier 2 writes in 55 days).

**Practical implication:** the two-tier architecture has effectively been Tier-1-only since June 15. All calibration/Brier/go-live data collected between June 15 and Aug 8 reflects that degraded state, not the intended design — don't treat the Aug 8 NO-GO verdict as meaningful evidence either way. Re-run go-live check only after several weeks of confirmed genuine Tier 2 activity.

**Aug 12, 2026 session:** a `resolver_overdue` alert on two stuck Kalshi trades led to root-causing a resolver bug that's been live since May: `api.kalshi.com` (the assumed production host, believed unreachable due to "Railway DNS issues") isn't actually a real Kalshi hostname at all — it never resolves, anywhere. The real prod host is `https://external-api.kalshi.com`. Fixed in `config.py` and `kalshi_resolution.py` (resolution now checks prod before the demo API, instead of the reverse). Also fixed the Dockerfile, which never copied `scripts/` into the deployed image — `railway ssh` + any `scripts/*.py` tool has silently never worked. Full account and a flagged-but-not-fixed related issue (scanning/execution still runs against the demo API) in `lloyd-backlog.md` → Recent History → Aug 12 entry. Not yet redeployed/verified live — see that entry for what "verified" means so far (tests only).

**Known documentation gap:** Nothing in this file, the backlog, or git commit messages fully accounts for what happened between March 21 and July 23 beyond what's reconstructed in the Decisions Log below from commit messages and `.specstory` session titles. If picking this project back up, it's worth skimming `.specstory/history/` for that window before assuming this doc is complete.

**Not yet confirmed as of this writing (verify before relying on it):**
- Current portfolio state (cash, exposure, open positions, resolved trade count, go-live check verdict) — check `/api/data` or `uv run python -m lloyd.dashboard` via `railway ssh`, don't assume the March 2026 figures in old handoff versions still apply
- Whether P10 (prompt anchoring possibly suppressing trade signals) has resolved itself now that RSS feeds and Tier 1 are both healthy — needs a few cycles of post-fix data
- Whether the GitHub Actions runner persists `.healthcheck-state.json` between runs (affects alert dedup/recovery detection — see `lloyd-monitoring-reference.md`)
- `lloyd.db` at 6.9GB and an unexplained 832MB backup file — see backlog I10, not urgent but unaddressed

**Blocking issues:** None known.

---

## WHAT I NEED HELP WITH

<!-- Replace this with your actual request each time -->

[Your specific question, task, or problem goes here]

---

## DECISIONS LOG

Entries through March 21, 2026 are from the original handoff series and are historical record — still valid unless superseded by a later entry. Entries from April onward are reconstructed from git commit history, `.specstory` session logs, `lloyd-monitoring-reference.md`, and `lloyd-backlog.md` — dates are as accurate as those sources allow but weren't logged turn-by-turn at the time.

**Through March 21, 2026 (original log):**
- LLM tier system: GPT-4o for Tier 1 screening, Claude Sonnet for deep analysis (later superseded — see July 2026 entries)
- Gemini disabled — `google-generativeai` SDK deprecated (later found to already be migrated to `google.genai` in code; actual blocker was billing, not SDK — see July 23 entry)
- GPT-5 unavailable at deployment; GPT-4o acting as primary Tier 1 model (superseded — see July 23 entry)
- Railway over Hetzner for deployment (existing account, better DX, ~$8-12/mo)
- Cross-platform arbitrage tracked as signal only, not traded (resolution divergence risk too high)
- Go-live criteria: Brier < 0.20, 100+ resolved trades, positive ROI, calibration error < 5%, 30 days stable, max drawdown < 30%
- Stage 1 API decisions: raw httpx over SDKs, plain sqlite3 over aiosqlite, `LLOYD_` env prefix, Kalshi prices are FixedPointDollars strings (not cents), Kalshi liquidity field deprecated (always 0), categories live in `lloyd/common/categories.py`
- Stage 1 architecture: `lloyd/common/` holds shared models, retry decorator, category normalization. Scanner outputs ranked `ScanResult` list. Only scanner-passed markets feed into prediction.
- Stage 2: `Article`/`NewsBundle` in `lloyd/common/models.py` (avoids circular imports); LLMs use official SDKs; `alpha=0.3` original ensemble weight (`final = (1-alpha)*market_price + alpha*ensemble_probability`); model strings config-driven; `ResearchCache.hash_query` public; `EnsemblePipeline.run()` returns `list[EnsemblePrediction]`; `_call_api` returns `tuple[str,int,int]`; `ClaudeSonnetPredictor` uses safe content-block extraction; Reddit/asyncpraw deferred (backlog R1)
- Stage 3: `PortfolioState` shared model; `TradeSignal`/`ExecutionResult` in `lloyd/execution/base.py`; `Executor` ABC; `limit_price` = `final_probability` for buy_yes, `1 - final_probability` for buy_no; `PaperExecutor` accepts optional exchange clients; large-move threshold 10pp; live executors are stubs (`NotImplementedError`); `pnl` written NULL on open, populated by Stage 4 resolver
- Stage 4: `OVERALL_CATEGORY_SENTINEL = ""` (SQLite UNIQUE workaround); `model_scores.period_type` ('alltime'/'rolling'); `kelly_adherence` 0–1 (1.0 = perfect); Monte Carlo drawdown returns `None` (not 0.0) under 50 trades; CLI entry points `python -m lloyd.dashboard`, `python -m lloyd.go_live`
- Stage 5: GDELT disabled entirely (RSS-only, later found RSS was also unset — see July 23); prediction cycle every 6h; max 40 prediction candidates; RSS feed timeout 5s, semaphore capped at 3; Railway Volume at `/data`; Prompt v1.0 → v1.1 (Mar 17, softened market-price anchoring); `model_prob` field added to logs; DEBUG logs dropped by Railway at infra level (use INFO); web dashboard on port 8080, `/api/data` + `/health`; `LLOYD_DATABASE_PATH`/`LLOYD_LOG_PATH` must point to `/data/` (container filesystem is wiped on redeploy — this misconfiguration lost all paper trading data from March 17–20); Kalshi 5xx crash bug fixed (graceful degrade to Polymarket-only); **I6 matcher decoupling resolved March 21** — matcher moved to `_matcher_job()`, runs every 6h via thread pool, unblocks `run_scan_cycle` and `_price_check_job`; `LLOYD_MAX_DAYS_TO_RESOLUTION` lowered 90→45 to accelerate resolved-trade accumulation toward go-live criteria (risk limits unchanged)

**April 2026 (reconstructed from git log + `.specstory` history):**
- **Apr 7** — GitHub Actions health-check system deployed (`lloyd_healthcheck.py` + `.github/workflows/lloyd-healthcheck.yml`), pulled forward after a resolver silent-failure bug caused undetected losses over ~2 weeks. Same day: Polymarket CLOB fallback added to resolver, dashboard API settled-trade status filter fixed, `RESOLVER_LOOKBACK_DAYS` variable wired into alert text, resolver lookback bumped to 10 days for Kalshi settlement delay.
- **Apr 12** — Paper cash balance bug fixed: now includes realized P&L from settled trades (previously cash balance didn't reflect closed positions correctly).
- **Apr 30 – May 2** — Health check correctly flagged resolver silent failure on trades 13/14/16 (Kalshi markets stuck open past close date due to a DNS error against `api.kalshi.com`). Two fix attempts (URL prefix correction, then forcing the resolver to always hit the live API with the correct URL) landed Apr 30; root cause fixed May 2 by making `kalshi_resolution_base_url` configurable (defaults to `demo-api.kalshi.co`) to work around the Railway DNS issue. Kalshi market resolution failures isolated so one bad market doesn't block others (May 1). **Correction (Aug 12, 2026):** the "Railway DNS issue" diagnosis was wrong — `api.kalshi.com` simply isn't a real Kalshi hostname and never resolves, from anywhere. The May 2 fix masked the crash by defaulting to the demo API but broke resolution correctness in the process. Real production host is `https://external-api.kalshi.com`. See `lloyd-backlog.md` → Recent History → Aug 12 entry for the actual fix.

**May 2026:**
- **May 15-16** — Health check hardening: `_build_api_data` moved to executor thread, healthcheck timeout bumped to 60s; event loop now yields every 10 pages during market fetch so the health server doesn't block; Polymarket Gamma API 422 pagination edge case handled gracefully.
- **May 27-29** — Notifier reliability pass: contract-based deployment autotriage alerts, Kalshi fallback resolution + stuck-trade recovery, `/api/data` fetch retries with delivery-error surfacing, secrets read at runtime with diagnostics, health server bound to Railway's `PORT` automatically. **~May 29 — the health check system was reworked into the current tiered autotriage design** (`routine_digest` / `autotriage_report` / `escalation` / `recovery_report` message types, dynamic pipeline/scan-dead thresholds, three-metric cost-spike check). This redesign wasn't reflected in `lloyd-monitoring-reference.md` until it was reconstructed and corrected against source on July 23 — see that file's own History section for the full account.
- **May 28** — README, PRD, and `.env.example` updated to reflect May 2026 ops (git commit `c284369`) — but that update still pointed at `lloyd-handoff-stage5_3.md` and `lloyd-backlog-0317.md`, which is part of how the dead references this consolidation just fixed came about.

**June 2026:**
- **Jun 6-7** — Kalshi API-first resolver with escalation deduplication; `pipeline_stuck` threshold aligned with the actual prediction interval; resolver now runs immediately on startup instead of waiting for the first scheduled interval.
- **Jun 18** — Stage 3 fixes: skip trades when a market already has an open position; contrary-position blocks logged separately for clarity. Gemini Tier 1 re-enabled with a calibration prompt and `buy_yes`-specific alpha (`LLOYD_BUY_YES_ALPHA=0.15` vs `LLOYD_MARKET_CONDITIONED_ALPHA=0.30` for buy_no/no_trade) — this is the re-enablement attempt that later turned out to still be failing silently on billing (see July 23). Railway container has been running continuously off this deploy since.

**July 2026:**
- **Jul 22** — GPT-5 cost tracking fixed for `gpt-5.6-luna` pricing (per-1M, not per-1K); `LLOYD_RSS_FEEDS` documented in `.env.example` after being found unset in production (root cause of `context_quality` always `'none'`).
- **Jul 23** — See "Recent History" at the top of `lloyd-backlog.md` for the full account: Gemini billing fix, GPT-5.6 Luna swap, RSS feeds populated (9 feeds), `NoDecode` parsing bug fix, monitoring doc reconstructed and then corrected against source. First confirmed critical cost-spike autotriage escalation under the new system (2.34× 7-day median) — plausibly explained by the same-day changes (real Gemini billing, real RSS token cost, restored Tier 2 escalations) but not fully confirmed against `/api/data` cost-by-day figures as of this writing.

**August 2026:**
- **Aug 8 (session 1)** — Documentation consolidation: merged the fragmented backlog files (`-0317`, `-0723`) into a single `lloyd-backlog.md`; reconstructed this handoff file to replace the missing `stage5`/`5.2`/`5_3`/`5_4` series; fixed dead references in `README.md` and `lloyd-prd.md`. No code changes this session.
- **Aug 8 (session 2)** — Ran the official go-live check for the first time since the July 23 fixes: **NO-GO**, weakest criteria calibration_error (0.2166 vs 0.05 threshold) and sample_size (79 vs 100). Investigating the calibration trend (pulled directly from `/api/data`'s `model_scores`, which already contains daily historical snapshots) showed overall Brier/calibration numbers frozen since ~Jul 16 and every `model_scores` row labeled `gpt-4o` only — no Gemini/Luna/Claude rows ever appeared, despite the July 23 fix. Traced via `railway ssh` + direct `predictions` table query: Gemini and Luna were both genuinely healthy and active, but **`claude-sonnet-4-20250514` had zero predictions since June 15** — 53 days — while `/api/data` was actively reporting `tier2_used: 1` on markets that same day. `railway logs | grep prediction_failed | grep claude` (not `/data/lloyd.log`, which turned out to be dead config — see I9) showed the real cause: Anthropic retired that dated model snapshot, every call 404'd, `except Exception` swallowed it and returned `None`, and a separate bug in `ensemble.py` set `tier2_used = True` unconditionally regardless of the result — so the failure was invisible everywhere it would normally surface. Confirmed `claude-sonnet-5` as a valid, cheaper, better-benchmarked replacement via `client.models.list()` against the live key (introductory pricing $2/$10 per 1M through 2026-08-31, then $3/$15). Fixed directly in code (not a Railway dashboard var this time, learning from the July 23 GPT-5 experience): `lloyd/config.py` `claude_model` default → `claude-sonnet-5`, cost tracking → `0.002`/`0.010` per 1k; `lloyd/prediction/ensemble.py` `tier2_used = tier2_result is not None`. Committed and pushed. Also found and logged, not yet fixed: dead `LLOYD_LOG_PATH`/`log_path` config (I9), `lloyd.db` grown to 6.9GB plus an unexplained 832MB April 7 backup (I10). Full account: `lloyd-backlog.md` → Recent History → Aug 8 entry, and P11/P12.

---

## LIVE TRADING ACTIVATION CHECKLIST (do not skip steps)

Pre-requisites — all must be true before flipping `LIVE_TRADING_ENABLED=true`:
1. `python -m lloyd.go_live` outputs `VERDICT: READY` (all six criteria pass) — run via `railway ssh` + `uv run`, or check `/api/data` for the underlying metrics
2. Live executor stubs implemented and integration-tested on demo/sandbox environments
3. Polymarket wallet funded and CLOB credentials configured
4. Kalshi production API key configured (not demo)
5. Capital ramp plan confirmed: start at 10% of intended capital for first 50 live trades
6. Slippage monitoring in place: compare paper `executed_price` to live `executed_price` per trade

Live trading ramp (from PRD):
- 10% of intended capital → first 50 live trades
- 25% → if paper-vs-live slippage aligns within 1pp
- 50% → after 50 more trades
- 100% → after another 50 trades and continued alignment
