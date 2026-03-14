# Prediction Market Trading Bot — Product Requirements Document

**Project codename:** Lloyd  
**Author:** [Your Name]  
**Date:** March 12, 2026  
**Status:** Pre-development  
**Primary tool:** Claude Code (greenfield build) → Cursor (iteration/debugging)

---

## 1. Vision and constraints

Lloyd is an AI-powered prediction market trading bot that identifies mispriced contracts on Polymarket and Kalshi, estimates true probabilities using a multi-model LLM ensemble, and places trades when confidence and edge are high. It begins as a paper trading system and graduates to live trading only after meeting strict statistical go-live criteria.

**Hard constraints:**

- Solo developer, Python 3.11+
- Infrastructure budget: under $50/month during paper trading, under $170/month live
- Paper trading first — no real capital deployed until go-live criteria are met
- Monolith-first architecture: plain Python with LLM-as-function calls, no multi-agent frameworks
- All data stored in SQLite (WAL mode) until a migration to Postgres is justified
- Deploy on Railway (existing account)

**Non-goals for v1:**

- High-frequency or sub-second trading
- Market making / providing liquidity
- Cross-platform arbitrage as a primary strategy (tracked as a signal, not traded)
- Mobile interface or web dashboard
- Supporting platforms beyond Polymarket and Kalshi

---

## 2. Architecture overview

Lloyd is a single async Python application with five modular components that run sequentially on a scheduled loop. LLMs are called as stateless functions — not as agents — only where they add genuine value (research synthesis and probability estimation). All deterministic logic (scanning, risk sizing, order management, scheduling) is pure Python.

**Core loop (runs every 30 minutes during market hours):**

```
MarketScanner → ResearchEngine → PredictionModel → RiskSizer → TradeExecutor
                                                                    ↓
                                                            PostmortemAnalyzer
                                                        (runs on market resolution)
```

**Component responsibilities:**

| Component | Role | Uses LLM? |
|-----------|------|-----------|
| MarketScanner | Fetches active markets from both platforms, filters by liquidity/volume/time-to-resolution, flags candidates | No |
| ResearchEngine | For flagged markets, retrieves relevant news via GDELT/RSS, summarizes context | Yes (Tier 1) |
| PredictionModel | Generates probability estimates using tiered multi-model ensemble, compares to market price | Yes (Tier 1 + 2) |
| RiskSizer | Calculates position size via quarter-Kelly, enforces risk limits, blocks bad trades | No |
| TradeExecutor | Places orders (paper or live) on the appropriate platform, logs execution details | No |
| PostmortemAnalyzer | On market resolution, records outcome, updates per-model Brier scores, generates learnings | Yes (lightweight) |

**LLM tier system:**

| Tier | Models | Use case | Monthly cost (est.) |
|------|--------|----------|-------------------|
| Tier 1 — Screening | Gemini 3.1 Pro (free tier) + GPT-5 | Bulk probability estimates on 100+ markets/day | $10–15 |
| Tier 2 — Deep analysis | Claude Sonnet 4.6 | Full RAG prediction on 15–30 high-edge markets/day | $15–25 |
| Ensemble aggregation | None (pure math) | Trimmed mean weighted by per-model Brier scores | $0 |

---

## 3. Build stages

The project is divided into four stages. Each stage produces a working, testable increment. Do not begin a later stage until the current stage is functional and tested.

---

### Stage 1: Foundation — Data collection and market scanning

**Goal:** Connect to both platform APIs, pull live market data, store it in SQLite, and run a scanner that filters markets by exploitability criteria.

**Deliverables:**

1. **Project scaffolding**
   - Python project with `pyproject.toml` (use `uv` for dependency management)
   - Directory structure: `lloyd/` package with `scanner/`, `research/`, `prediction/`, `risk/`, `execution/`, `postmortem/`, `common/` submodules
   - `config.py` using Pydantic Settings for environment-based configuration
   - SQLite database with WAL mode, initial schema (see below)
   - Logging via `structlog` (JSON output for machine parsing)

2. **Polymarket integration** (`lloyd/scanner/polymarket.py`)
   - Connect to Gamma API (no auth required) for market discovery
   - Pull active markets: condition_id, question, description, outcomes, current prices, volume, liquidity, end_date
   - Connect to CLOB API for order book depth on candidate markets
   - Store raw market snapshots in `markets` table with timestamp
   - Handle pagination, rate limiting (respect 60/s sustained), and error recovery

3. **Kalshi integration** (`lloyd/scanner/kalshi.py`)
   - Connect to Kalshi REST API v2 with RSA key authentication
   - Pull active events and markets: ticker, title, category, yes_price, no_price, volume, open_interest, close_time
   - Store in same `markets` table with platform discriminator
   - Use demo API (`demo-api.kalshi.co`) for development and paper trading
   - Handle rate limits (10 writes/s basic tier)

4. **Market scanner** (`lloyd/scanner/scanner.py`)
   - Filter markets by: minimum volume (>$10K lifetime), minimum liquidity (>$1K in top 3 book levels), time-to-resolution (7–90 days), price range (exclude >$0.95 and <$0.05 — too close to resolution)
   - Score remaining markets by exploitability: category weight (entertainment/weather = high, finance = low), volume-to-liquidity ratio, price movement volatility over last 24h
   - Output: ranked list of candidate markets for research phase
   - Target: scan completes in under 60 seconds across both platforms

5. **Cross-platform market matching** (`lloyd/scanner/matcher.py`)
   - Fuzzy match markets across Polymarket and Kalshi by question text similarity (use `rapidfuzz` library)
   - Store matched pairs in `market_pairs` table
   - Calculate cross-platform price divergence — log for analysis, do not trade on it

**Database schema (initial):**

```sql
CREATE TABLE markets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,           -- 'polymarket' or 'kalshi'
    platform_id TEXT NOT NULL,        -- condition_id or ticker
    question TEXT NOT NULL,
    category TEXT,
    current_price REAL,               -- YES price, 0-1
    volume REAL,
    liquidity REAL,
    open_interest REAL,
    close_date TEXT,                   -- ISO 8601
    raw_data TEXT,                     -- JSON blob of full API response
    fetched_at TEXT NOT NULL,          -- ISO 8601
    UNIQUE(platform, platform_id, fetched_at)
);

CREATE TABLE market_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polymarket_id TEXT,
    kalshi_id TEXT,
    similarity_score REAL,
    price_divergence REAL,
    matched_at TEXT NOT NULL
);

CREATE TABLE scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    exploitability_score REAL,
    scan_timestamp TEXT NOT NULL,
    passed_filter INTEGER DEFAULT 0   -- boolean
);
```

**Acceptance criteria for Stage 1:**
- Running `python -m lloyd.scanner` pulls live data from both platforms and populates the database
- Scanner filters 500+ markets down to 30–80 candidates in under 60 seconds
- Market matcher identifies at least 10 cross-platform pairs from current active markets
- All API errors are caught, logged with structlog, and retried with exponential backoff
- Unit tests cover API response parsing with mocked data
- Integration test confirms end-to-end scan with live APIs

---

### Stage 2: Research and prediction pipeline

**Goal:** Build the LLM-powered research and prediction system. For each candidate market, retrieve relevant news, generate probability estimates from multiple models, and compare to market price to identify edge.

**Deliverables:**

1. **News retrieval engine** (`lloyd/research/news.py`)
   - GDELT API integration: query by keywords extracted from market question, retrieve articles from last 7 days, extract title/source/date/tone
   - RSS feed aggregator: parse feeds from 15–20 major news sources (AP, Reuters, BBC, NPR, NYT, WSJ, Bloomberg, etc.)
   - Reddit API integration (free tier): search relevant subreddits for sentiment on market topics
   - Deduplicate results by headline similarity
   - Output: structured context bundle per market (list of articles with title, source, date, sentiment score, snippet)
   - Cache results in `research_cache` table to avoid redundant API calls within 2-hour window

2. **LLM prediction interface** (`lloyd/prediction/llm.py`)
   - Abstract `Predictor` base class with `predict(market, context) -> PredictionResult` interface
   - Concrete implementations: `GeminiPredictor`, `GPT5Predictor`, `ClaudeSonnetPredictor`
   - Each predictor uses a standardized prompt template (see below) and returns: probability estimate (0.0–1.0), confidence level (1–5), reasoning summary, model name, token count
   - Structured output parsing — require JSON response with schema validation
   - Retry logic with fallback (if primary model fails, use backup)
   - Token usage tracking per call for cost monitoring

3. **Prediction prompt template** (stored in `lloyd/prediction/prompts/`)
   - System prompt establishes the forecasting persona and calibration requirements
   - User prompt includes: market question, current market price, resolution criteria, time to resolution, news context bundle, base rate information if available
   - Explicitly instruct the model to: (a) consider both sides, (b) state confidence, (c) account for market price as informative but potentially wrong, (d) output a precise probability, not a range
   - Variant prompts for different market categories (politics, weather, entertainment, sports)

4. **Tiered ensemble pipeline** (`lloyd/prediction/ensemble.py`)
   - Tier 1 screening: run Gemini (free) + GPT-5 on all candidate markets from scanner
   - Flag markets where either model diverges from market price by >5 percentage points
   - Tier 2 deep analysis: for flagged markets only, run Claude Sonnet with extended context (full news bundle, not summaries)
   - Ensemble aggregation: trimmed mean of all model estimates, weighted by each model's rolling Brier score for that market category
   - Apply market-conditioned mixing: `final = 0.7 × market_price + 0.3 × ensemble_estimate` (adjustable alpha)
   - Output: `Prediction` record with final probability, edge vs. market, individual model estimates, and all reasoning

5. **Prediction storage**

```sql
CREATE TABLE predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    model_name TEXT NOT NULL,
    probability REAL NOT NULL,
    confidence INTEGER,
    reasoning TEXT,
    tokens_used INTEGER,
    cost_usd REAL,
    prompt_version TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE ensemble_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    ensemble_probability REAL NOT NULL,
    market_price REAL NOT NULL,
    edge REAL NOT NULL,              -- ensemble_probability - market_price
    alpha REAL DEFAULT 0.7,
    final_probability REAL NOT NULL, -- after market-conditioned mixing
    model_predictions TEXT,          -- JSON array of individual model predictions
    trade_signal TEXT,               -- 'buy_yes', 'buy_no', 'no_trade'
    created_at TEXT NOT NULL
);
```

**Prediction prompt template (core version):**

```
You are a calibrated probability forecaster. Your task is to estimate the
true probability of an event, given available evidence.

EVENT: {market_question}
RESOLUTION CRITERIA: {resolution_criteria}
CURRENT MARKET PRICE: {current_price} (this reflects crowd consensus but may be wrong)
TIME TO RESOLUTION: {days_remaining} days
CATEGORY: {category}

RECENT NEWS AND CONTEXT:
{news_context_bundle}

Instructions:
1. Consider evidence for and against the event occurring.
2. Identify any information the market may not have priced in.
3. Consider base rates for similar events if applicable.
4. The current market price is informative — crowds are usually right — but
   you should deviate if the evidence warrants it.
5. Be precise. Do not hedge with ranges. Give a single probability.

Respond in JSON format only:
{
  "probability": <float 0.0 to 1.0>,
  "confidence": <int 1-5, where 5 is very confident>,
  "reasoning": "<2-3 sentence explanation of your key factors>",
  "evidence_for": "<key evidence supporting the event>",
  "evidence_against": "<key evidence against the event>",
  "market_disagree_reason": "<why you differ from market, or 'aligned'>"
}
```

**Acceptance criteria for Stage 2:**
- Pipeline generates predictions for 50+ markets in a single run
- Each prediction includes complete reasoning and cost tracking
- Ensemble produces a single final probability with documented edge calculation
- Total LLM cost for a full daily run (100 Tier 1 + 25 Tier 2 predictions) stays under $2
- News retrieval returns relevant articles for 90%+ of markets (spot-check 20 markets manually)
- All predictions stored with full provenance (model, prompt version, input context hash)
- Unit tests cover prompt construction, response parsing, and ensemble aggregation math

---

### Stage 3: Risk management and paper trading

**Goal:** Add Kelly sizing, risk limits, and a complete paper trading simulation that tracks realistic execution. Begin the 12-week evaluation period.

**Deliverables:**

1. **Risk sizing engine** (`lloyd/risk/sizer.py`)
   - Quarter-Kelly position sizing: `fraction = 0.25 × (edge / odds)` where edge = `final_probability - market_price` and odds = `(1 / market_price) - 1`
   - Hard limits: max 5% of bankroll per position, max 20% total exposure, max 3 positions per market category
   - Minimum edge threshold: do not trade if |edge| < 3 percentage points (configurable)
   - Minimum confidence threshold: do not trade if ensemble confidence average < 3.0
   - Trade blocking: reject trades where any individual model in the ensemble assigns >80% probability to the opposite direction (disagreement kill switch)
   - Output: `TradeSignal` with direction, size, limit price, and all risk metrics

2. **Paper trading executor** (`lloyd/execution/paper.py`)
   - Simulate order execution with realistic parameters: 300ms latency, 0.5% slippage on market orders, partial fill simulation based on order book depth
   - Track simulated P&L in USDC (Polymarket) and USD (Kalshi)
   - Apply actual fee structures: Polymarket 0.10% taker, Kalshi's nonlinear formula
   - Maintain a simulated portfolio with position tracking, unrealized P&L, and cash balance
   - Starting simulated bankroll: $10,000
   - Log every simulated trade with timestamp, market snapshot, prediction details, and risk metrics

3. **Live API integration stubs** (`lloyd/execution/polymarket_live.py`, `lloyd/execution/kalshi_live.py`)
   - Implement the same `Executor` interface as the paper trader
   - Polymarket: use py-clob-client for CLOB order placement, EIP-712 signing
   - Kalshi: use kalshi-python SDK, demo environment during testing
   - These modules exist as code but are disabled by a `LIVE_TRADING_ENABLED=false` environment variable
   - Include safety checks: max order size, daily loss limit, require manual confirmation for orders over $500

4. **Scheduler** (`lloyd/main.py`)
   - Main entry point that runs the full pipeline on a configurable schedule
   - Default: full scan every 30 minutes, 6am–11pm UTC
   - Lightweight price-check every 5 minutes for markets with open positions (detect large moves)
   - Graceful shutdown handling (SIGTERM)
   - Health check endpoint (simple HTTP) for Railway monitoring
   - Configurable via environment variables

5. **Paper trading storage**

```sql
CREATE TABLE trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    ensemble_prediction_id INTEGER REFERENCES ensemble_predictions(id),
    platform TEXT NOT NULL,
    direction TEXT NOT NULL,          -- 'buy_yes' or 'buy_no'
    quantity REAL NOT NULL,
    limit_price REAL,
    executed_price REAL,
    slippage REAL,
    fee REAL,
    is_paper INTEGER DEFAULT 1,
    status TEXT DEFAULT 'open',       -- 'open', 'filled', 'partial', 'cancelled'
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    pnl REAL                          -- set on resolution
);

CREATE TABLE portfolio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cash_balance REAL NOT NULL,
    total_exposure REAL NOT NULL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    num_open_positions INTEGER,
    snapshot TEXT                      -- JSON of all positions
);
```

**Acceptance criteria for Stage 3:**
- Full pipeline runs end-to-end: scan → research → predict → size → paper trade
- Risk sizer correctly blocks trades that violate any limit (test with edge cases)
- Paper trades include realistic slippage and fee deductions
- Portfolio tracker maintains accurate running P&L
- Scheduler runs reliably for 72+ hours without crashes
- All trades logged with complete audit trail linking back to predictions and market data
- Live executor modules compile and pass interface tests but are gated behind `LIVE_TRADING_ENABLED`

---

### Stage 4: Postmortem system and evaluation dashboard

**Goal:** Build the learning system that evaluates predictions against outcomes, tracks per-model calibration, and produces a decision-quality dashboard for go/no-go evaluation.

**Deliverables:**

1. **Outcome tracker** (`lloyd/postmortem/resolver.py`)
   - Poll both platforms for resolved markets (Polymarket: check resolution status via Gamma API; Kalshi: check settlement via REST API)
   - On resolution: record actual outcome, calculate P&L on any open paper trades, update trade status
   - Run every 15 minutes

2. **Calibration analyzer** (`lloyd/postmortem/calibration.py`)
   - Calculate Brier score per model, per category, and overall
   - Generate calibration data: bin predictions into deciles (0–10%, 10–20%, etc.), compare predicted probability to actual outcome rate per bin
   - Track rolling 30-day Brier scores to detect model drift
   - Update model weights in the ensemble based on recent performance
   - Flag categories where a specific model consistently outperforms (e.g., Claude better at politics, Gemini better at sports)

3. **Performance metrics** (`lloyd/postmortem/metrics.py`)
   - Win rate (% of trades with positive P&L)
   - ROI after all fees
   - Calibration error (mean absolute difference between predicted and actual probability per decile)
   - Pseudo-Sharpe ratio: mean trade return / stdev of trade returns × sqrt(trades_per_year)
   - Kelly adherence: actual position sizes vs. theoretical optimal
   - Max drawdown (peak-to-trough of cumulative P&L)
   - Category-level breakdowns of all above metrics

4. **CLI dashboard** (`lloyd/postmortem/dashboard.py`)
   - Terminal-based summary using `rich` library
   - Show: current portfolio, open positions, today's trades, overall P&L, Brier scores by model and category, calibration plot (ASCII), top wins and losses
   - Exportable to markdown for review

5. **Go-live evaluation** (`lloyd/postmortem/go_live_check.py`)
   - Automated check of all go-live criteria:
     - Overall Brier score < 0.20 across 100+ predictions
     - Positive simulated ROI after all fees
     - Calibration error < 5%
     - 100+ resolved paper trades
     - Max drawdown < 30% (Monte Carlo simulation at 95th percentile)
     - 30+ consecutive days of stable operation
   - Outputs a clear YES/NO with supporting data
   - If NO, identifies the weakest criteria and suggests areas for improvement

6. **Postmortem storage**

```sql
CREATE TABLE outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    platform TEXT NOT NULL,
    outcome TEXT NOT NULL,            -- 'yes', 'no', 'void'
    resolved_at TEXT NOT NULL
);

CREATE TABLE model_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    category TEXT,
    brier_score REAL NOT NULL,
    calibration_error REAL,
    num_predictions INTEGER,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    UNIQUE(model_name, category, period_end)
);
```

**Acceptance criteria for Stage 4:**
- Resolved markets automatically update trade P&L and Brier scores
- Calibration analysis produces per-model, per-category breakdowns
- Dashboard renders a complete view of system performance in terminal
- Go-live check correctly evaluates all criteria and produces actionable output
- Ensemble weights update dynamically based on rolling model performance
- All metrics are auditable — any number can be traced back to underlying predictions and outcomes

---

## 4. Technology stack

| Layer | Choice | Rationale |
|-------|--------|-----------|
| Language | Python 3.11+ | Ecosystem, LLM library support, solo dev velocity |
| Package manager | uv | Fast, modern, replaces pip/pip-tools |
| HTTP client | httpx | Async support, better API than requests |
| Database | SQLite (WAL mode) | Zero-config, ACID, sufficient for single-process |
| LLM clients | anthropic, openai, google-generativeai | Official SDKs, structured output support |
| News data | GDELT API, feedparser (RSS) | Free, comprehensive, no API key needed |
| Reddit | asyncpraw | Free tier, 100 queries/min |
| Polymarket | py-clob-client | Official SDK for CLOB trading |
| Kalshi | kalshi-python | Official SDK (v2.1.4) |
| Fuzzy matching | rapidfuzz | Fast string similarity for market matching |
| Logging | structlog | Structured JSON logging |
| CLI display | rich | Terminal tables, charts, formatting |
| Config | pydantic-settings | Typed env var configuration |
| Testing | pytest + pytest-asyncio | Standard, async support |
| Scheduling | APScheduler | Lightweight, cron-like scheduling |
| Deployment | Railway | Existing account, git-push deploys |

---

## 5. Configuration and environment variables

```env
# Platform API credentials
POLYMARKET_PRIVATE_KEY=           # Ethereum wallet private key for signing
POLYMARKET_API_KEY=               # CLOB API key
POLYMARKET_API_SECRET=            # CLOB API secret
POLYMARKET_API_PASSPHRASE=        # CLOB API passphrase

KALSHI_API_KEY_ID=                # RSA key ID
KALSHI_RSA_PRIVATE_KEY_PATH=      # Path to RSA private key file
KALSHI_BASE_URL=https://demo-api.kalshi.co  # Use demo for paper trading

# LLM API keys
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
GOOGLE_AI_API_KEY=

# Trading configuration
LIVE_TRADING_ENABLED=false         # MUST be explicitly set to true for live trading
PAPER_BANKROLL=10000               # Starting paper trading balance
MAX_POSITION_PCT=0.05              # Max 5% of bankroll per position
MAX_EXPOSURE_PCT=0.20              # Max 20% total exposure
MIN_EDGE_THRESHOLD=0.03            # Minimum 3pp edge to trade
MIN_CONFIDENCE=3.0                 # Minimum ensemble confidence (1-5)
KELLY_FRACTION=0.25                # Quarter-Kelly sizing
MARKET_CONDITIONED_ALPHA=0.7       # Weight on market price in final estimate

# Scanner configuration
MIN_VOLUME=10000                   # Minimum lifetime volume ($)
MIN_LIQUIDITY=1000                 # Minimum top-3 book liquidity ($)
MIN_DAYS_TO_RESOLUTION=7
MAX_DAYS_TO_RESOLUTION=90
SCAN_INTERVAL_MINUTES=30
PRICE_CHECK_INTERVAL_MINUTES=5

# Infrastructure
DATABASE_PATH=./lloyd.db
LOG_LEVEL=INFO
HEALTH_CHECK_PORT=8080
```

---

## 6. Go-live criteria (all must be met)

| Criterion | Threshold | Measurement |
|-----------|-----------|-------------|
| Prediction accuracy | Brier score < 0.20 | Over 100+ resolved predictions |
| Trade profitability | Positive ROI | After all simulated fees and slippage |
| Calibration quality | Error < 5% | Mean absolute error across decile bins |
| Sample size | 100+ resolved trades | Statistically meaningful |
| Drawdown risk | < 30% max drawdown | Monte Carlo at 95th percentile |
| System stability | 30+ consecutive days | No crashes or missed cycles |

**Live trading ramp:** 10% of intended capital for first 50 trades → 25% → 50% → 100%, upgrading only when paper-vs-live slippage aligns within 1 percentage point.

---

## 7. Risk register

| Risk | Likelihood | Impact | Mitigation |
|------|-----------|--------|------------|
| LLM probability estimates are poorly calibrated | Medium | High | Multi-model ensemble, rolling Brier tracking, Platt scaling |
| API breaking changes (Polymarket or Kalshi) | Medium | Medium | Version-pin SDKs, wrap all API calls in adapter layer |
| Exchange fees erode all edge | Medium | High | Only trade when edge > 3pp + estimated fees; track net-of-fee ROI |
| Resolution divergence on cross-platform pairs | Low | Critical | Do not trade cross-platform arb; use divergence as signal only |
| Model drift (LLM accuracy degrades over time) | Medium | Medium | Rolling 30-day Brier monitoring, automatic weight adjustment |
| Overfitting to paper trading conditions | Medium | Medium | Use realistic slippage/latency simulation; start live at 10% capital |
| Polymarket regulatory changes affect US access | Low | High | Monitor CFTC announcements; Kalshi as fallback platform |
| Insufficient market liquidity for exit | Medium | Medium | Max position size limits; prefer markets with >$50K volume |
