# Lloyd

A prediction market trading bot that connects to Polymarket and Kalshi, runs an LLM ensemble for probability estimates, paper-trades with Kelly sizing, and tracks calibration via postmortem analysis. Deployed on **Railway** with external health monitoring via **GitHub Actions**.

## Quick start

```bash
# Install dependencies
uv sync

# Configure
cp .env.example .env
# Edit .env — Kalshi credentials optional for read-only scanning; LLM keys required for predictions

# Run a single scan
uv run python -m lloyd scan

# Start the full scheduler (scan, prediction, resolver, dashboard HTTP server)
uv run python -m lloyd run
```

## Configuration

All environment variables are prefixed with `LLOYD_`.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLOYD_POLYMARKET_WALLET_KEY` | *(empty)* | Polymarket wallet private key (Stage 3) |
| `LLOYD_POLYMARKET_CLOB_KEY` | *(empty)* | Polymarket CLOB API key (Stage 3) |
| `LLOYD_POLYMARKET_CLOB_SECRET` | *(empty)* | Polymarket CLOB API secret (Stage 3) |
| `LLOYD_POLYMARKET_CLOB_PASSPHRASE` | *(empty)* | Polymarket CLOB passphrase (Stage 3) |
| `LLOYD_KALSHI_API_KEY_ID` | *(empty)* | Kalshi API key ID |
| `LLOYD_KALSHI_RSA_KEY_PATH` | *(empty)* | Path to Kalshi RSA private key PEM file |
| `LLOYD_KALSHI_BASE_URL` | `https://demo-api.kalshi.co/trade-api/v2` | Kalshi API base URL |
| `LLOYD_MIN_VOLUME` | `10000` | Minimum market volume to pass filter |
| `LLOYD_MIN_LIQUIDITY` | `1000` | Minimum liquidity (Polymarket only) |
| `LLOYD_MIN_DAYS_TO_RESOLUTION` | `7` | Minimum days until market close |
| `LLOYD_MAX_DAYS_TO_RESOLUTION` | `90` | Maximum days until market close |
| `LLOYD_SCAN_INTERVAL_MINUTES` | `30` | Scheduler interval |
| `LLOYD_DATABASE_PATH` | `./lloyd.db` | SQLite database file path |
| `LLOYD_LOG_LEVEL` | `INFO` | Logging level (`DEBUG` for console output) |
| `LLOYD_HEALTH_CHECK_PORT` | `8080` (local); **Railway `PORT`** when unset on Railway | HTTP port for `/health`, `/api/data`, dashboard |
| `LLOYD_KALSHI_RESOLUTION_BASE_URL` | `https://demo-api.kalshi.co` | Kalshi host for settlement lookups (see handoff for Railway DNS note) |

Full healthcheck / autotriage thresholds (used by `lloyd_healthcheck.py` and GitHub Actions) are documented in `.env.example` under **Health Check / Autotriage Defaults** (`PIPELINE_STUCK_HOURS`, `SCAN_DEAD_HOURS`, cost guards, etc.).

## Deployment & monitoring

**Railway:** `python -m lloyd run` binds the HTTP server to `PORT` automatically unless `LLOYD_HEALTH_CHECK_PORT` is set. Expose the service via Railway **Networking → Generate Domain**. Endpoints:

| Route | Purpose |
|-------|---------|
| `/health` | `{"status":"ok"}` — liveness |
| `/api/data` | Dashboard JSON (open trades, predictions, costs) |
| `/` | Web dashboard (`lloyd/dashboard.html`) |

Persist `LLOYD_DATABASE_PATH` and `LLOYD_LOG_PATH` on a volume (e.g. `/data/lloyd.db`, `/data/lloyd.log`). See `lloyd-handoff-stage5_3.md` for Railway env checklist.

**GitHub Actions — Lloyd Health Check** (`.github/workflows/lloyd-healthcheck.yml`):

- Cron every **6 hours** (`0 */6 * * *` UTC); **workflow_dispatch** for manual runs
- Runs `python lloyd_healthcheck.py` (contract-based autotriage; see script for message types)
- **Required repo secrets:** `LLOYD_BASE_URL` (domain only, no `/health` suffix), `SLACK_WEBHOOK_URL`
- **Optional:** `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- Healthy run: all checks pass → Slack receives JSON `routine_digest` with `"status_summary": "pass"`
- Escalations: critical findings, low-confidence findings, or high severity with `cost` / `functionality` risk tags → `escalation` payload (critical prefixed `[IMMEDIATE]` in Slack text)

Operations detail: **Operations runbook** in `lloyd-handoff-stage5_3.md`.

## Architecture

```
lloyd/
├── config.py               # Pydantic Settings, all env vars
├── db.py                    # SQLite WAL mode setup + schema + helpers
├── main.py                  # Entry point — CLI + APScheduler loop
├── common/
│   ├── models.py            # Market, ScanResult, MarketPair
│   ├── categories.py        # Tag slug → category normalization
│   └── retry.py             # Exponential backoff decorator
├── scanner/
│   ├── polymarket.py        # Gamma API client
│   ├── kalshi.py            # Kalshi REST v2 client
│   ├── scanner.py           # Filter pipeline + exploitability scoring
│   └── matcher.py           # Cross-platform fuzzy matching
├── research/                # Stage 2
├── prediction/              # Stage 2
├── risk/                    # Stage 3
├── execution/               # Stage 3
└── postmortem/              # Stage 4
```

**Scan cycle flow:** Fetch markets from both APIs concurrently → filter by volume, time-to-resolution, price, and liquidity → score by exploitability (category multiplier × volume score + extreme price bonus) → fuzzy-match cross-platform pairs → store in SQLite → print summary table.

## Tests

```bash
# Unit tests (no network required)
uv run pytest tests/ -v

# Integration tests (hits live APIs)
LLOYD_INTEGRATION_TESTS=1 uv run pytest tests/test_integration.py -v
```

## Related docs

| File | Contents |
|------|----------|
| `lloyd-prd.md` | Full PRD, schemas, go-live criteria |
| `lloyd-handoff-stage5_3.md` | Railway env, decisions log, **operations runbook** |
| `lloyd-backlog-0317.md` | Deferred work with data-gated triggers |
| `lloyd-log-reference.md` | Railway log events and cadence |

## Roadmap

| Stage | Scope | Status |
|-------|-------|--------|
| 1 | Market scanner, data collection, exploitability ranking | Done |
| 2 | LLM research + probability prediction | Done |
| 3 | Risk management + paper execution | Done |
| 4 | Postmortem, calibration, go-live check | Done |
| 5 | Railway deployment + paper trading evaluation | **In progress** |
