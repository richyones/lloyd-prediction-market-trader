---
name: Lloyd Stage 1 Build
overview: "Build Lloyd Stage 1: a Python application using httpx, SQLite, and APScheduler that fetches live market data from Polymarket (Gamma API) and Kalshi (REST v2), stores it, filters/scores markets by exploitability, and fuzzy-matches cross-platform pairs."
todos:
  - id: scaffold
    content: "Create project skeleton: pyproject.toml, directory tree, all __init__.py files, __main__.py entry points, empty stubs for future stages"
    status: completed
  - id: config
    content: Implement config.py (Pydantic Settings) and .env.example with LLOYD_ prefixed names
    status: completed
  - id: models
    content: Implement common/models.py (Market, ScanResult, MarketPair pydantic models)
    status: completed
  - id: retry
    content: Implement common/retry.py (exponential backoff decorator)
    status: completed
  - id: db
    content: Implement db.py (SQLite WAL setup + schema creation + insert/query helpers)
    status: completed
  - id: polymarket
    content: Implement scanner/polymarket.py (Gamma API client with pagination, include_tag, and double-parse)
    status: completed
  - id: kalshi
    content: Implement scanner/kalshi.py (Kalshi REST v2 client with cursor pagination)
    status: completed
  - id: scanner
    content: Implement scanner/scanner.py (filter pipeline + exploitability scoring + _normalize_category with tag mapping)
    status: completed
  - id: matcher
    content: Implement scanner/matcher.py (cross-platform fuzzy matching)
    status: completed
  - id: main
    content: Implement lloyd/main.py + lloyd/__main__.py + lloyd/scanner/__main__.py (entry points and APScheduler loop)
    status: completed
  - id: tests
    content: "Implement all tests: conftest.py, test_polymarket, test_kalshi, test_scanner, test_matcher, test_integration"
    status: completed
  - id: readme
    content: Write README.md with setup and usage instructions
    status: completed
isProject: false
---

# Lloyd Stage 1 -- Prediction Market Scanner

## Critical findings from API research

Before the file-by-file plan, four things the spec got wrong or that need design adjustment:

1. **Kalshi prices are NOT cents.** The current v2 API returns `FixedPointDollars` strings (e.g., `"0.5600"`) for `yes_bid_dollars`, `last_price_dollars`, etc. The old `response_price_units: usd_cent` is deprecated. We will parse the string to float directly -- no divide-by-100.

2. **Kalshi `liquidity_dollars` is deprecated** and always returns `"0.0000"`. The liquidity filter must be made optional -- it will only apply to Polymarket markets. Kalshi markets will pass the liquidity filter by default.

3. **Dropping `py-clob-client` and `kalshi-python` from dependencies.** Both are sync-only. `py-clob-client` hits the CLOB API (order books/trading), not the Gamma API we need. `kalshi-python` is legacy. Using `httpx` directly for both gives us native async, fewer dependencies, and full control over parsing. We can add trading SDKs back in Stage 3 when execution is built.

4. **Polymarket `category` field is always `None` on `/markets`.** Categories are tag-based, living on the parent event. Adding `include_tag=true` to the `/markets` query populates a `tags` array on each market with event-level tag objects. We extract the first matching tag slug and normalize it through `_normalize_category()`. See the mapping dict defined in the scanner.py section below.

---

## Decided: Polymarket `active` parameter

The Gamma `/markets` OpenAPI spec does not list `active` as a query parameter (it exists on `/events`). However, the response **does** include an `active` boolean field on each market.

**Approach:** Query with `closed=false` only. Post-filter in `_parse_market()`: skip any market where `raw.get("active")` is not `True`. This is safe and avoids relying on an undocumented query parameter.

---

## Build order

Files are built dependency-first. Each section covers: purpose, public API, and design notes.

### Phase 1: Skeleton and configuration (files 1-4)

#### 1. `pyproject.toml`

**Purpose:** uv-managed project definition with all dependencies.

**Dependencies (final list):**

```
httpx
pydantic>=2.0
pydantic-settings
structlog
rapidfuzz
apscheduler>=3.10
pytest
pytest-asyncio
python-dotenv
```

**Design notes:**

- `[project.scripts]` entry: `lloyd = "lloyd.main:cli"` for command-line invocation
- Python requires `>=3.11`
- No `py-clob-client` or `kalshi-python` -- raw httpx for both APIs

#### 2. `lloyd/config.py`

**Purpose:** Single source of truth for all configuration, loaded from environment variables.

**Public API:**

```python
class Settings(BaseSettings):
    # Polymarket (not needed for Stage 1 read-only, but documented)
    polymarket_wallet_key: str = ""
    polymarket_clob_key: str = ""
    polymarket_clob_secret: str = ""
    polymarket_clob_passphrase: str = ""

    # Kalshi
    kalshi_api_key_id: str = ""
    kalshi_rsa_key_path: str = ""
    kalshi_base_url: str = "https://demo-api.kalshi.co/trade-api/v2"

    # Scanner thresholds
    min_volume: float = 10_000
    min_liquidity: float = 1_000
    min_days_to_resolution: int = 7
    max_days_to_resolution: int = 90

    # Scheduler
    scan_interval_minutes: int = 30

    # Database
    database_path: str = "./lloyd.db"
    log_level: str = "INFO"

    model_config = SettingsConfigDict(env_file=".env", env_prefix="LLOYD_")

def get_settings() -> Settings: ...  # cached singleton
```

**Design notes:**

- All env vars prefixed with `LLOYD_` to avoid collisions (e.g., `LLOYD_MIN_VOLUME=10000`)
- Credentials default to empty strings so the app runs without them for read-only mode
- `get_settings()` uses `@lru_cache` for singleton behavior

#### 3. `.env.example`

**Purpose:** Documents every configuration variable with sensible defaults and comments.

**Exact contents:**

```
# ── Lloyd Configuration ──────────────────────────────────────────
# Copy this file to .env and fill in your values.
# All variables are prefixed with LLOYD_ to avoid collisions.

# ── Polymarket (read-only in Stage 1, no credentials needed) ──
# LLOYD_POLYMARKET_WALLET_KEY=
# LLOYD_POLYMARKET_CLOB_KEY=
# LLOYD_POLYMARKET_CLOB_SECRET=
# LLOYD_POLYMARKET_CLOB_PASSPHRASE=

# ── Kalshi ──
# LLOYD_KALSHI_API_KEY_ID=
# LLOYD_KALSHI_RSA_KEY_PATH=./kalshi_private_key.pem
LLOYD_KALSHI_BASE_URL=https://demo-api.kalshi.co/trade-api/v2

# ── Scanner Thresholds ──
LLOYD_MIN_VOLUME=10000
LLOYD_MIN_LIQUIDITY=1000
LLOYD_MIN_DAYS_TO_RESOLUTION=7
LLOYD_MAX_DAYS_TO_RESOLUTION=90

# ── Scheduler ──
LLOYD_SCAN_INTERVAL_MINUTES=30

# ── Database & Logging ──
LLOYD_DATABASE_PATH=./lloyd.db
LLOYD_LOG_LEVEL=INFO
```

#### 4. Directory tree -- all `__init__.py` files and future-stage stubs

**Created files (empty or minimal):**

- `lloyd/__init__.py` -- package marker, may contain `__version__`
- `lloyd/__main__.py` -- calls `cli()` from `lloyd.main` (enables `python -m lloyd`)
- `lloyd/common/__init__.py`
- `lloyd/scanner/__init__.py`
- `lloyd/scanner/__main__.py` -- calls `run_scan_cycle()` (enables `python -m lloyd.scanner`)
- `lloyd/research/__init__.py` -- empty, Stage 2
- `lloyd/prediction/__init__.py` -- empty, Stage 2
- `lloyd/risk/__init__.py` -- empty, Stage 3
- `lloyd/execution/__init__.py` -- empty, Stage 3
- `lloyd/postmortem/__init__.py` -- empty, Stage 4
- `tests/__init__.py`

**`lloyd/__main__.py` contents:**

```python
from lloyd.main import cli

if __name__ == "__main__":
    cli()
```

**`lloyd/scanner/__main__.py` contents:**

```python
import asyncio
from lloyd.main import run_scan_cycle

if __name__ == "__main__":
    asyncio.run(run_scan_cycle())
```

This means:
- `python -m lloyd` -- full CLI with subcommands (`scan`, `run`)
- `python -m lloyd.scanner` -- runs a single scan cycle directly

---

### Phase 2: Shared infrastructure (files 5-7)

#### 5. `lloyd/common/models.py`

**Purpose:** Shared Pydantic data models used across all modules.

**Public API:**

```python
class Market(BaseModel):
    platform: Literal["polymarket", "kalshi"]
    platform_id: str           # conditionId or ticker
    question: str
    category: str | None = None  # normalized canonical category
    current_price: float       # YES price, 0.0-1.0
    volume: float
    liquidity: float | None = None
    open_interest: float | None = None
    close_date: datetime | None = None
    raw_data: dict[str, Any]   # full API response for this market
    fetched_at: datetime       # set at fetch time

class ScanResult(BaseModel):
    market: Market
    exploitability_score: float
    passed_filter: bool
    scan_timestamp: datetime

class MarketPair(BaseModel):
    polymarket_market: Market
    kalshi_market: Market
    similarity_score: float
    price_divergence: float
    matched_at: datetime
```

**Design notes:**

- `raw_data` stored as dict in Python, serialized to JSON string for SQLite
- `liquidity` is `Optional` because Kalshi always returns `"0.0000"` (deprecated field)
- `open_interest` is `Optional` because Polymarket may not always report it
- `close_date` used for time-to-resolution filtering
- `category` stores the normalized canonical name (e.g., `"entertainment"`, `"politics"`) after running through `_normalize_category()`

#### 6. `lloyd/common/retry.py`

**Purpose:** Async decorator for exponential backoff on transient HTTP errors.

**Public API:**

```python
def with_retry(
    max_retries: int = 3,
    base_delay: float = 1.0,
    retryable_exceptions: tuple = (httpx.HTTPStatusError, httpx.ConnectError),
) -> Callable:
    """Decorator. Retries on 429/5xx HTTPStatusError and ConnectError.
    Delay doubles each retry: 1s, 2s, 4s."""
```

**Design notes:**

- Only retries `HTTPStatusError` when status is 429 or 5xx (not 400/401/404)
- Uses `asyncio.sleep` between retries
- Logs each retry attempt with structlog (attempt number, delay, error)
- Returns the decorator pattern: `@with_retry()` or `@with_retry(max_retries=5)`

#### 7. `lloyd/db.py`

**Purpose:** SQLite connection management, schema initialization, and data access helpers.

**Public API:**

```python
def get_connection(db_path: str) -> sqlite3.Connection:
    """Opens connection with WAL mode, returns it."""

def init_db(conn: sqlite3.Connection) -> None:
    """Creates tables if they don't exist."""

def insert_markets(conn: sqlite3.Connection, markets: list[Market]) -> None:
    """Bulk-inserts Market objects. Serializes raw_data to JSON.
    Uses INSERT OR IGNORE for the UNIQUE constraint."""

def insert_scan_results(conn: sqlite3.Connection, results: list[ScanResult]) -> None:
    """Inserts scan results, linking to market rows by platform+platform_id+fetched_at."""

def insert_market_pairs(conn: sqlite3.Connection, pairs: list[MarketPair]) -> None:
    """Inserts matched pairs."""
```

**Design notes:**

- Plain `sqlite3` from stdlib -- no ORM, no async wrapper. SQLite writes are fast enough that blocking briefly is fine, and async sqlite wrappers add complexity for no real gain at this scale.
- WAL mode set via `PRAGMA journal_mode=WAL` on every connection open
- `PRAGMA foreign_keys=ON` enabled
- Schema exactly as specified (three tables: `markets`, `market_pairs`, `scan_results`)
- `insert_markets` uses `executemany` with `INSERT OR IGNORE` so re-fetching the same market at the same timestamp is idempotent

---

### Phase 3: API clients (files 8-9)

#### 8. `lloyd/scanner/polymarket.py`

**Purpose:** Async client that fetches all active markets from the Polymarket Gamma API.

**Public API:**

```python
class PolymarketClient:
    def __init__(self, http_client: httpx.AsyncClient | None = None): ...

    @with_retry()
    async def _fetch_page(self, offset: int, limit: int) -> list[dict]: ...

    async def fetch_all_markets(self) -> list[Market]: ...

    def _parse_market(self, raw: dict) -> Market | None: ...
```

**Key implementation details:**

- **Endpoint:** `GET https://gamma-api.polymarket.com/markets?closed=false&include_tag=true&limit=100&offset={n}`
- **`include_tag=true`** is critical: without it, the `tags` array is absent and we get no category data.
- **`active` filtering:** No `active` query param. Instead, `_parse_market()` checks `raw.get("active") is True` and skips markets where it is falsy.
- **Pagination:** Offset-based. Fetch pages until `len(results) < limit`.
- **Double-parse pitfall:** `outcomePrices` is a JSON-encoded string inside JSON. Example: the API returns `"outcomePrices": "[\"0.85\",\"0.15\"]"`. Must call `json.loads(raw["outcomePrices"])` to get the actual list, then `float(prices[0])` for the YES price.
- **Category extraction:** Extract the first recognized tag slug from the `tags` array, pass through `_normalize_category()` (defined in scanner.py but also available as a standalone utility). Store the raw tag slug and let the scanner normalize it.
- **Field mapping:**
  - `platform_id` = `conditionId`
  - `question` = `question`
  - `category` = first recognized tag slug from `tags`, normalized (see mapping below)
  - `current_price` = first element of parsed `outcomePrices`
  - `volume` = `volumeNum` (already numeric in Gamma response)
  - `liquidity` = `liquidityNum` (already numeric)
  - `close_date` = parse `endDateIso`
  - `raw_data` = entire raw dict
- **Error handling:** `_parse_market` returns `None` for markets that can't be parsed (missing fields, malformed prices). These are logged and skipped, not crashed on.
- **Logging:** Total markets fetched, pages traversed, parse failures, total fetch duration.

**Design notes:**

- Accepts an optional `httpx.AsyncClient` for dependency injection (testability)
- Creates its own client if none provided, with a 30s timeout
- The Gamma API has no auth and generous rate limits, but we still use the retry decorator as a safety net
- Category normalization happens in `_parse_market()` using the shared `normalize_category()` function from scanner.py

#### 9. `lloyd/scanner/kalshi.py`

**Purpose:** Async client that fetches all open markets from the Kalshi REST v2 API.

**Public API:**

```python
class KalshiClient:
    def __init__(
        self,
        base_url: str,
        api_key_id: str = "",
        rsa_key_path: str = "",
        http_client: httpx.AsyncClient | None = None,
    ): ...

    def _sign_request(self, method: str, path: str, timestamp_ms: int) -> str: ...

    @with_retry()
    async def _fetch_page(self, cursor: str = "") -> tuple[list[dict], str]: ...

    async def fetch_all_markets(self) -> list[Market]: ...

    def _parse_market(self, raw: dict) -> Market | None: ...
```

**Key implementation details:**

- **Endpoint:** `GET {base_url}/markets?status=open&limit=200`
- **Pagination:** Cursor-based. Response contains `{"markets": [...], "cursor": "..."}`. Keep fetching until `cursor` is empty string.
- **Price parsing:** `last_price_dollars` is a `FixedPointDollars` string like `"0.5600"`. Parse with `float(raw["last_price_dollars"])`. Falls back to midpoint of `yes_bid_dollars` and `yes_ask_dollars` if `last_price_dollars` is missing/zero.
- **Field mapping:**
  - `platform_id` = `ticker`
  - `question` = `title` (deprecated but still returned; `yes_sub_title` as fallback)
  - `category` = `None` (Kalshi has no category field; scored as `"unknown"` = 1.0x multiplier)
  - `current_price` = parsed `last_price_dollars`
  - `volume` = `float(raw["volume_fp"])` (string like `"15234.00"`)
  - `liquidity` = `None` (field is deprecated, always `"0.0000"`)
  - `open_interest` = `float(raw["open_interest_fp"])`
  - `close_date` = parse `close_time` (ISO 8601 datetime)
  - `raw_data` = entire raw dict
- **Auth:** If `api_key_id` and `rsa_key_path` are provided, sign requests with RSA-PSS/SHA256. If not provided (or key file missing), make unauthenticated requests and log a warning. The demo API returns market data without auth for `GET /markets`.
- **RSA signing:** `timestamp_ms + method + path` -> sign with PSS padding -> base64 encode -> set headers `KALSHI-ACCESS-KEY`, `KALSHI-ACCESS-TIMESTAMP`, `KALSHI-ACCESS-SIGNATURE`.

**Design notes:**

- Kalshi has no `category` on markets. We could infer from `series_ticker` prefixes (e.g., `KXBTC` = crypto), but this is fragile. For Stage 1, category defaults to `None` and the scanner treats it as `"unknown"` (multiplier 1.0). Stage 2 can add event-level category fetching.
- The `title` field is deprecated but still populated. Using it is fine for Stage 1 matching. Stage 2 can switch to event-level descriptions.
- `limit=200` per page (Kalshi max is 1000, but 200 is a reasonable balance)

---

### Phase 4: Scanner logic (files 10-11)

#### 10. `lloyd/scanner/scanner.py`

**Purpose:** Applies filter pipeline and exploitability scoring to a list of Market objects.

**Public API:**

```python
def normalize_category(tags: list[dict] | None, fallback: str | None = None) -> str | None:
    """Extracts a canonical category from a list of Polymarket tag dicts,
    or returns fallback. Used by both polymarket.py and scanner.py."""

class MarketScanner:
    def __init__(self, settings: Settings): ...

    def scan(self, markets: list[Market]) -> list[ScanResult]:
        """Full pipeline: filter -> score -> sort -> return."""

    def _filter_volume(self, markets: list[Market]) -> list[Market]: ...
    def _filter_time(self, markets: list[Market]) -> list[Market]: ...
    def _filter_price(self, markets: list[Market]) -> list[Market]: ...
    def _filter_liquidity(self, markets: list[Market]) -> list[Market]: ...

    def _score(self, market: Market) -> float: ...
```

**Category normalization mapping (derived from live Gamma API data):**

This is the `_normalize_category()` function's lookup table. Tag slugs from the Gamma API `tags` array are matched in priority order (first match wins):

```python
TAG_SLUG_TO_CATEGORY: dict[str, str] = {
    # Sports
    "sports": "sports",
    "soccer": "sports",
    "nba": "sports",
    "EPL": "sports",
    "serie-a": "sports",
    "nfl": "sports",
    "mlb": "sports",
    "nhl": "sports",
    "tennis": "sports",
    "golf": "sports",
    "mma": "sports",
    "f1": "sports",
    "cricket": "sports",

    # Politics
    "politics": "politics",
    "elections": "politics",
    "global-elections": "politics",
    "world-elections": "politics",
    "us-presidential-election": "politics",
    "trump": "politics",
    "trump-presidency": "politics",
    "courts": "politics",
    "congress": "politics",
    "columbia-election": "politics",

    # World events
    "world": "world_events",
    "geopolitics": "world_events",
    "ukraine": "world_events",
    "foreign-policy": "world_events",
    "world-affairs": "world_events",
    "israel": "world_events",

    # Crypto
    "crypto": "crypto",
    "airdrops": "crypto",

    # Entertainment / culture
    "pop-culture": "entertainment",

    # Finance
    "finance": "finance",
    "stocks": "finance",
    "ipos": "finance",
    "pre-market": "finance",
    "economy": "finance",
    "business": "finance",

    # Tech (mapped to crypto for Stage 1 -- revisit in Stage 2)
    "tech": "crypto",
    "ai": "crypto",

    # Weather (in multiplier table but no tags observed yet)
    "weather": "weather",
    "climate": "weather",
}
```

`normalize_category()` iterates a market's `tags` list, looks up each tag's `slug` in this dict, and returns the first match. If no tags match, returns `None` (treated as `"unknown"`, multiplier 1.0).

**Exploitability scoring constants:**

```python
CATEGORY_MULTIPLIERS: dict[str, float] = {
    "entertainment": 1.5,
    "weather": 1.5,
    "world_events": 1.3,
    "sports": 1.0,
    "crypto": 1.0,
    "politics": 0.8,
    "finance": 0.5,
}
DEFAULT_MULTIPLIER = 1.0
```

**Key implementation details:**

- **Filter pipeline** (applied in order, each step logs input/output count):
  1. Volume: `market.volume > settings.min_volume`
  2. Time: `settings.min_days_to_resolution <= days_until_close <= settings.max_days_to_resolution`. Markets with no `close_date` are excluded.
  3. Price: `0.05 < market.current_price < 0.95` (strict inequality -- boundary values are filtered out)
  4. Liquidity: `market.liquidity > settings.min_liquidity` **if** `market.liquidity is not None`. Markets with `liquidity=None` (i.e., all Kalshi markets) pass this filter automatically.
- **Scoring formula:**
  - `category_mult = CATEGORY_MULTIPLIERS.get(market.category, DEFAULT_MULTIPLIER)` if category is not None, else `DEFAULT_MULTIPLIER`
  - `volume_score = math.log10(market.volume) / 7.0` (normalized: log10(10M) ~ 7, so most scores fall in 0-1)
  - `extreme_price_bonus = abs(0.5 - market.current_price) * 0.5`
  - `final = category_mult * (volume_score + extreme_price_bonus)`
- **Output:** List of `ScanResult` sorted by `exploitability_score` descending. `passed_filter=True` for all (only filtered markets reach scoring).

**Design notes:**

- The scanner is synchronous -- it operates on in-memory lists, no I/O
- The `/7.0` normalization for volume_score is a pragmatic choice: `log10(10,000,000) = 7`, and very few prediction markets exceed $10M volume. This keeps volume_score roughly in [0.5, 1.2] for typical markets.
- `normalize_category()` is a module-level function (not a method) so it can be imported by both `polymarket.py` and `scanner.py`

#### 11. `lloyd/scanner/matcher.py`

**Purpose:** Fuzzy-matches markets across Polymarket and Kalshi to find cross-platform pairs.

**Public API:**

```python
class MarketMatcher:
    SIMILARITY_THRESHOLD: float = 80.0
    MIN_QUESTION_LENGTH: int = 10

    def match(
        self,
        polymarket_markets: list[Market],
        kalshi_markets: list[Market],
    ) -> list[MarketPair]: ...
```

**Key implementation details:**

- Filters out markets where `len(question) < MIN_QUESTION_LENGTH` before comparison
- Uses `rapidfuzz.fuzz.token_sort_ratio` for comparison (token-order-invariant, handles "Will X happen?" vs "X happening" well)
- Iterates all Poly x Kalshi pairs (O(n*m)). For Stage 1 volumes (~hundreds of markets per platform), this is fine.
- Matches where `similarity > SIMILARITY_THRESHOLD` are kept
- `price_divergence = abs(poly.current_price - kalshi.current_price)`
- Returns `list[MarketPair]` sorted by `price_divergence` descending (biggest divergence = most interesting)

**Design notes:**

- O(n*m) is acceptable for Stage 1. If market counts grow past ~5000 per platform, we would switch to `rapidfuzz.process.cdist` for vectorized comparison or pre-filter by category. Not needed yet.
- `token_sort_ratio` chosen over `ratio` or `partial_ratio` because prediction market questions often have the same words in different order across platforms.

---

### Phase 5: Entry points (files 12-13)

#### 12. `lloyd/main.py`

**Purpose:** CLI entry point. Runs a single scan cycle or starts the scheduler loop.

**Public API:**

```python
async def run_scan_cycle() -> None:
    """One full cycle: fetch -> filter -> score -> match -> store -> print summary."""

def print_summary(results: list[ScanResult], pairs: list[MarketPair]) -> None:
    """Prints top-20 table and any cross-platform pairs found."""

def cli() -> None:
    """Parses args: 'scan' for one-shot, 'run' for scheduler loop."""
```

**Key implementation details:**

- `run_scan_cycle()` orchestrates:
  1. Create `PolymarketClient` and `KalshiClient`
  2. Fetch markets concurrently with `asyncio.gather`
  3. Combine into single list, pass to `MarketScanner.scan()`
  4. Pass platform-separated lists to `MarketMatcher.match()`
  5. Store everything in SQLite via `db.py` helpers
  6. Call `print_summary()`
- `print_summary()` prints a formatted table (simple string formatting, no rich dependency):

```
Rank | Platform    | Question                                             | Price | Volume    | Score
---- | ----------- | ---------------------------------------------------- | ----- | --------- | -----
   1 | polymarket  | Will Trump win the 2026 midterm election for Re...   | 0.65  | 1,234,567 | 2.31
```

- `cli()` uses `argparse`:
  - `python -m lloyd scan` -- runs `run_scan_cycle()` once via `asyncio.run()`
  - `python -m lloyd run` -- starts `AsyncIOScheduler` with `run_scan_cycle` as an interval job
- structlog configured at startup: JSON output in production, console output when `LOG_LEVEL=DEBUG`

#### 13. `lloyd/__main__.py` and `lloyd/scanner/__main__.py`

Already covered in Phase 1, file 4. Confirming both are in the build:

- **`lloyd/__main__.py`** -- `from lloyd.main import cli; cli()` -- enables `python -m lloyd`
- **`lloyd/scanner/__main__.py`** -- `asyncio.run(run_scan_cycle())` -- enables `python -m lloyd.scanner` for a single scan cycle without the full CLI

---

### Phase 6: Tests (files 14-20)

#### 14. `tests/conftest.py`

**Purpose:** Shared fixtures for all test modules.

**Fixtures:**

- `mock_polymarket_response` -- realistic JSON list mimicking Gamma API output (3-5 markets with various edge cases: missing category, outcomePrices as JSON string, null endDate, tags array with real tag objects)
- `mock_kalshi_response` -- realistic JSON dict mimicking Kalshi response (3-5 markets with FixedPointDollars prices, cursor pagination)
- `tmp_db` -- creates a temporary SQLite database with schema initialized, yields connection, cleans up
- `sample_markets` -- list of synthetic `Market` objects covering all filter boundary conditions
- `settings` -- `Settings` instance with test-appropriate defaults

#### 15. `tests/test_polymarket.py`

**Tests:**

- `test_fetch_single_page` -- mock returns < limit results, no pagination
- `test_fetch_pagination` -- mock returns limit results on page 1, fewer on page 2
- `test_outcome_prices_double_parse` -- verifies `"[\"0.85\",\"0.15\"]"` is correctly parsed to `0.85`
- `test_missing_outcome_prices` -- market with null outcomePrices is skipped gracefully
- `test_retry_on_429` -- mock returns 429 twice then 200, verifies success after retries
- `test_field_mapping` -- verifies conditionId -> platform_id, question, volumeNum -> volume, etc.
- `test_inactive_market_filtered` -- market with `active: false` is skipped by `_parse_market()`
- `test_category_from_tags` -- market with `tags: [{"slug": "pop-culture", ...}]` maps to `category="entertainment"`

#### 16. `tests/test_kalshi.py`

**Tests:**

- `test_fetch_single_page` -- mock returns empty cursor, no further pages
- `test_cursor_pagination` -- mock returns cursor on page 1, empty on page 2
- `test_price_parsing` -- verifies `"0.5600"` FixedPointDollars string -> `0.56` float
- `test_price_fallback_to_midpoint` -- when `last_price_dollars` is `"0.0000"`, falls back to `(yes_bid + yes_ask) / 2`
- `test_field_mapping` -- verifies ticker -> platform_id, title -> question, volume_fp -> volume
- `test_no_auth_graceful` -- when no credentials configured, requests go through without auth headers and don't crash

#### 17. `tests/test_scanner.py`

**Tests:**

- `test_volume_filter_boundary` -- market at exactly MIN_VOLUME should fail (strict >), market at MIN_VOLUME + 1 passes
- `test_time_filter_boundaries` -- 6 days fails, 7 days passes, 90 days passes, 91 days fails
- `test_price_filter_boundaries` -- 0.05 fails, 0.06 passes, 0.94 passes, 0.95 fails
- `test_liquidity_filter_none_passes` -- market with `liquidity=None` passes (Kalshi case)
- `test_liquidity_filter_value` -- market with liquidity below threshold fails
- `test_scoring_category_multiplier` -- entertainment market scores 1.5x vs finance at 0.5x (same volume/price)
- `test_scoring_extreme_price_bonus` -- market at 0.10 gets higher bonus than market at 0.45
- `test_empty_input` -- empty list returns empty list, no crash
- `test_all_filtered_out` -- all markets fail filters, returns empty list
- `test_normalize_category_mapping` -- verifies tag slug -> canonical category for all entries in `TAG_SLUG_TO_CATEGORY`
- `test_normalize_category_no_match` -- tags with unrecognized slugs return `None`

#### 18. `tests/test_matcher.py`

**Tests:**

- `test_obvious_match` -- "Will Biden win 2026 midterms?" vs "Biden 2026 midterm election" matches (similarity > 80)
- `test_obvious_non_match` -- "Bitcoin price above 100k" vs "Will it rain in NYC tomorrow" does not match
- `test_threshold_boundary` -- craft two strings with ~80% similarity, verify the threshold is applied correctly
- `test_price_divergence_calculation` -- poly at 0.65 and kalshi at 0.55 gives divergence of 0.10
- `test_short_question_skipped` -- questions under 10 characters are excluded from matching
- `test_empty_input` -- empty list from either platform returns no pairs

#### 19. `tests/test_integration.py`

**Purpose:** End-to-end smoke tests that hit real APIs. Skipped when credentials / network are unavailable.

**Pattern:**

```python
import pytest

requires_network = pytest.mark.skipif(
    os.environ.get("LLOYD_INTEGRATION_TESTS") != "1",
    reason="Set LLOYD_INTEGRATION_TESTS=1 to run integration tests",
)

@requires_network
@pytest.mark.asyncio
async def test_polymarket_fetch_live():
    """Fetch a single page from the real Gamma API and verify we get Market objects."""

@requires_network
@pytest.mark.asyncio
async def test_kalshi_fetch_live():
    """Fetch a single page from the real Kalshi demo API and verify we get Market objects."""

@requires_network
@pytest.mark.asyncio
async def test_full_scan_cycle():
    """Run a complete scan cycle against real APIs, verify results are stored in SQLite."""
```

**Tests:**

- `test_polymarket_fetch_live` -- fetches 1 page (limit=10) from Gamma, asserts >= 1 Market returned, checks `platform == "polymarket"` and `current_price` is in [0, 1]
- `test_kalshi_fetch_live` -- fetches 1 page from Kalshi demo, asserts >= 1 Market returned, checks `platform == "kalshi"` and price is in [0, 1]
- `test_full_scan_cycle` -- runs `run_scan_cycle()` with a temp DB, verifies rows exist in all three tables
- `test_polymarket_tags_populated` -- fetches with `include_tag=true`, verifies at least some markets have non-None `category`

**Design notes:**

- Gated by `LLOYD_INTEGRATION_TESTS=1` env var, not by credential checks. This keeps them off by default in CI but easy to enable locally.
- Uses a temporary database (via `tmp_path` fixture) to avoid polluting the real DB
- Kept deliberately lightweight -- just enough to verify the API contract hasn't changed

---

### Phase 7: Documentation (file 20)

#### 20. `README.md`

**Contents:**

- What Lloyd is (one paragraph)
- Quick start (uv sync, copy .env.example, run scan)
- Configuration reference (table of all env vars with `LLOYD_` prefix)
- Architecture overview (which modules do what)
- Running tests (`pytest` for unit, `LLOYD_INTEGRATION_TESTS=1 pytest tests/test_integration.py` for integration)
- Stage roadmap (Stage 1 = this, Stages 2-4 = future)

---

## Design decisions and tradeoffs

| Decision | Rationale |
|----------|-----------|
| httpx over SDKs | Both SDKs are sync-only. httpx gives us async, fewer deps, and direct control over parsing. SDKs can be added back for Stage 3 trading. |
| Plain sqlite3 over aiosqlite | SQLite writes at this scale (hundreds of rows per scan) take <10ms. An async wrapper adds complexity for no measurable benefit. |
| Gamma `/markets` with `include_tag=true` | Gets category data from tags without needing a second `/events` call. `category` field on markets is always `None`. |
| `closed=false` only, post-filter `active` | The `active` query param is not in the `/markets` OpenAPI spec. The `active` field is on the response, so we filter client-side. |
| `token_sort_ratio` over other fuzzy methods | Prediction market questions reorder words across platforms. Token-sort handles "Will X do Y?" vs "Y by X" naturally. |
| Category defaults to `None`/unknown for Kalshi | No category field on Kalshi markets. Series-ticker heuristics would be fragile. Better to score them at 1.0x and add proper categorization in Stage 2 via event metadata. |
| `LLOYD_` env prefix | Avoids collision with other tools' env vars. Every config var becomes `LLOYD_MIN_VOLUME`, `LLOYD_DATABASE_PATH`, etc. |
| Volume filter uses strict `>` | "Greater than MIN_VOLUME" means the threshold itself is excluded. This is the more conservative choice -- markets exactly at the boundary are marginal. |
| Integration tests gated by env var | Simple, explicit opt-in. No credential sniffing. Works the same in local dev and CI. |

## Future considerations (not Stage 1)

1. **Kalshi event-level category:** In Stage 2, fetch `/events` from Kalshi to get richer metadata (category, full description) and join with markets. This would improve both scoring and matching quality.
2. **Tag slug mapping maintenance:** The `TAG_SLUG_TO_CATEGORY` dict will need periodic updates as Polymarket adds new tag slugs. Consider fetching `/tags` at startup to detect unrecognized slugs and log warnings.
3. **Matcher performance:** If market counts grow past ~5000, switch to `rapidfuzz.process.cdist` or pre-filter by category before cross-comparison.
