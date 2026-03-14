# Lloyd

A prediction market trading bot that connects to Polymarket and Kalshi, pulls live market data, stores it in SQLite, and runs a scanner that filters and ranks markets by exploitability. This is **Stage 1** — no LLM predictions, no trading, no risk management yet.

## Quick start

```bash
# Install dependencies
uv sync

# Configure
cp .env.example .env
# Edit .env if you have Kalshi API credentials (optional for Stage 1)

# Run a single scan
uv run python -m lloyd scan

# Or run just the scanner directly
uv run python -m lloyd.scanner

# Start the scheduler (scans every 30 minutes)
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

## Roadmap

| Stage | Scope | Status |
|-------|-------|--------|
| 1 | Market scanner, data collection, exploitability ranking | **Current** |
| 2 | LLM-powered research + probability prediction | Planned |
| 3 | Risk management + order execution | Planned |
| 4 | Postmortem analysis + performance tracking | Planned |
