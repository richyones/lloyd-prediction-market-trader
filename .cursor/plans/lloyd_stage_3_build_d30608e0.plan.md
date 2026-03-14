---
name: Lloyd Stage 3 Build
overview: Stage 3 adds risk management (Kelly sizing + hard limits), paper trading with simulated execution (slippage, fees), portfolio tracking in SQLite, live executor stubs, a 5-minute price-check loop, health check HTTP endpoint, and SIGTERM handling. No new dependencies; builds on existing db, config, and main loop.
todos: []
isProject: false
---

# Lloyd Stage 3: Risk Management and Paper Trading — Implementation Plan

## Dependency and placement decisions

### Where `PortfolioState` should live

**Recommendation: define `PortfolioState` in [lloyd/common/models.py](lloyd/common/models.py).**

- **Why not `risk/sizer.py`:** If execution (paper executor) imported from risk, we’d have execution → risk. That’s acceptable but couples execution to risk.
- **Why not `execution/base.py`:** Putting it there would force risk → execution for a data container. You called that “questionable dependency direction for a risk module,” and it would make the risk package depend on execution.
- **Why common:** Both `risk/sizer.py` and `execution/paper.py` already depend on `lloyd/common` (models, config, etc.). Adding `PortfolioState` (and optionally a small `Trade`-like type for in-memory open positions) in `common/models.py` keeps risk and execution independent and reuses the existing “shared data shapes” role of common.

**Concrete:** Add to `common/models.py` a dataclass `PortfolioState(cash_balance, total_exposure, positions: list[dict])` and the method `exposure_by_category(self, category: str) -> int`. Position dicts: `{trade_id, market_id, platform, direction, quantity, entry_price, category}`. No need for a separate `common/portfolio.py` unless you prefer to group all “portfolio” types there later.

### Health check HTTP server

**Recommendation: use raw `asyncio.start_server` as specified.**

- No new dependency; PRD says no web framework.
- Implementation: bind to `0.0.0.0` and `LLOYD_HEALTH_CHECK_PORT`, register a callback that: reads one request (e.g. up to 4KB), checks that the first line is `GET /health HTTP/1.x`, then sends `HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{"status":"ok"}` and closes the connection (or keep-alive if you want to reuse the socket; for health checks, close is fine).
- Railway will hit `GET /health` on the configured port; a single response is enough. No need for a separate process or thread.
- Run the server in the same event loop as the scheduler (e.g. create the server in `_main()` before the “keep alive” loop and store the server object so the SIGTERM handler can close it).

### Linking trades to ensemble predictions (required)

The `trades` table has `ensemble_prediction_id INTEGER REFERENCES ensemble_predictions(id)`. Stage 4 needs this to trace resolved trades back to the prediction that generated them (calibration audit trail). The pipeline change is **required**, not optional.

**Implementation:**

- In [lloyd/db.py](lloyd/db.py): add `insert_ensemble_prediction(conn, ep) -> int` that inserts a single row and returns `conn.lastrowid`.
- In [lloyd/prediction/ensemble.py](lloyd/prediction/ensemble.py): add `ensemble_prediction_id: int | None = None` to `EnsemblePrediction` (optional on the model until set; always set before the object is used for trading). In the pipeline loop, replace the batch insert with the single-insert helper, set `ep.ensemble_prediction_id = insert_ensemble_prediction(self._conn, ep)`, then append to results.
- In [lloyd/execution/base.py](lloyd/execution/base.py): `TradeSignal.ensemble_prediction_id` is `int`, not `int | None` — callers always supply the id when building a signal from a prediction that passed the pipeline.

### Resolving platform and platform_id for TradeSignal

`EnsemblePrediction` has `market_id` but not `platform` or `platform_id`. The executor and TradeSignal need `platform` and `platform_id` (condition_id for Polymarket, ticker for Kalshi).

**Recommendation:** Add in [lloyd/db.py](lloyd/db.py) a helper such as:

- `get_market_info(conn, market_id: int) -> tuple[str, str, str | None] | None`  
Returns `(platform, platform_id, category)` or `None` if not found.  
Query: `SELECT platform, platform_id, category FROM markets WHERE id = ?`.

Then in `main.py`, before the risk/execution loop, for each prediction with `trade_signal != 'no_trade'`, call `get_market_info(conn, prediction.market_id)`. If `None`, log and skip. Otherwise build `TradeSignal` with `platform`, `platform_id`, and `category` from this lookup (and `ensemble_prediction_id` from the prediction if we add it as above).

---

## Build order and file-by-file plan

### 1. Database schema and helpers — [lloyd/db.py](lloyd/db.py)

**Purpose:** Add `trades` and `portfolio` tables and all Stage 3 DB helpers.

**Schema additions (append to `SCHEMA`):**

- `trades`: columns as in PRD (`id`, `market_id`, `ensemble_prediction_id`, `platform`, `direction`, `quantity`, `limit_price`, `executed_price`, `slippage`, `fee`, `is_paper`, `status`, `large_move_flagged`, `opened_at`, `closed_at`, `pnl`). Use `REFERENCES markets(id)` and `REFERENCES ensemble_predictions(id)`. `ensemble_prediction_id` is required (NOT NULL); populated from the pipeline’s single-insert helper.
- `portfolio`: columns as in PRD (`id`, `timestamp`, `cash_balance`, `total_exposure`, `unrealized_pnl`, `realized_pnl`, `num_open_positions`, `snapshot`).

**New functions:**

- `insert_ensemble_prediction(conn, ep) -> int` — insert one `ensemble_predictions` row, return `conn.lastrowid`. Required for Stage 3 so every trade can store `ensemble_prediction_id` and Stage 4 can trace back.
- `get_market_info(conn, market_id: int) -> tuple[str, str, str | None] | None` — returns `(platform, platform_id, category)`.
- `get_open_paper_trades(conn) -> list[Trade]` — `SELECT` from `trades` where `status = 'open' AND is_paper = 1`; join `markets` if needed to get `platform_id`; map rows to a simple dataclass.
- `flag_large_move(conn, trade_id: int) -> None` — `UPDATE trades SET large_move_flagged = 1 WHERE id = ?`.
- `insert_trade(conn, ...) -> int` — insert one trade row, return `lastrowid`. Parameters match the table columns. For price-check, either add `platform_id` to `trades` or have `get_open_paper_trades` join `markets` and return `platform_id` in the Trade dataclass (plan recommends the join so schema matches PRD).
- **Trade dataclass:** Define in `db.py` (or `common/models.py`) with at least: `id`, `market_id`, `platform`, `platform_id`, `direction`, `quantity`, `executed_price`. Return type of `get_open_paper_trades`.

**Design note:** Keep using synchronous `sqlite3` and existing `get_connection`/`init_db`; `init_db` will run the updated `SCHEMA` so both new tables are created.

---

### 2. Config additions — [lloyd/config.py](lloyd/config.py)

**Purpose:** Add all Stage 3 settings without changing existing ones.

**Add to `Settings` (and use correct attribute names for existing):**

- `LLOYD_LIVE_TRADING_ENABLED: bool = False`
- `LLOYD_PAPER_BANKROLL: float = 10_000.0`
- `LLOYD_PAPER_SLIPPAGE_PCT: float = 0.005`
- `LLOYD_POLYMARKET_FEE_RATE: float = 0.001`
- `LLOYD_KALSHI_FEE_RATE: float = 0.07`
- `LLOYD_MAX_POSITION_PCT: float = 0.05`
- `LLOYD_MAX_EXPOSURE_PCT: float = 0.20`
- `LLOYD_KELLY_FRACTION: float = 0.25`
- `LLOYD_MIN_CONFIDENCE: float = 3.0`
- `LLOYD_PRICE_CHECK_INTERVAL_MINUTES: int = 5`
- `LLOYD_LARGE_MOVE_THRESHOLD: float = 0.10`
- `LLOYD_HEALTH_CHECK_PORT: int = 8080`

**Do not re-add:** `min_edge_threshold` (already exists as `min_edge_threshold: float = 0.03`; sizer will use `settings.min_edge_threshold`).

**Pydantic:** With `env_prefix="LLOYD_"`, attribute names can be the env names without prefix (e.g. `live_trading_enabled`, `paper_bankroll`) or you keep the full names; ensure they match what the rest of the code uses (e.g. `paper_bankroll`, `max_position_pct`).

---

### 3. Executor interface and shared types — [lloyd/execution/base.py](lloyd/execution/base.py) (new)

**Purpose:** Single place for the executor contract and the types used by both risk and execution.

**Definitions:**

- **TradeSignal** (Pydantic): `market_id`, `ensemble_prediction_id: int` (required), `platform`, `platform_id`, `direction`, `quantity`, `limit_price`, `category`. All as in PRD.
- `**ExecutionResult`** (Pydantic): `trade_id`, `market_id`, `platform`, `direction`, `quantity`, `limit_price`, `executed_price`, `slippage`, `fee`, `is_paper`, `status`, `opened_at` (ISO 8601).
- `**Executor**` (ABC):
  - `async def execute(self, signal: TradeSignal) -> ExecutionResult`
  - `async def get_current_price(self, market_id: int, platform: str, platform_id: str) -> float | None`
  - `def is_live(self) -> bool`

**Design:** `PortfolioState` is not defined here; it lives in `common/models.py` so risk doesn’t depend on execution. Only `TradeSignal` and `ExecutionResult` are in base; executors and main import these from `execution.base`.

---

### 4. PortfolioState — [lloyd/common/models.py](lloyd/common/models.py)

**Purpose:** Shared portfolio snapshot type for sizer and paper executor.

**Add:**

- Dataclass `**PortfolioState`**: `cash_balance: float`, `total_exposure: float`, `positions: list[dict]`. Each dict: `trade_id`, `market_id`, `platform`, `direction`, `quantity`, `entry_price`, `category`.
- Method `**exposure_by_category(self, category: str) -> int`**: count positions where `pos.get("category") == category`.

**Design:** Use a simple dict for positions so we don’t introduce a new Pydantic model for a single internal structure; the sizer only needs counts and category. If you prefer type safety, you can use a small TypedDict or a dataclass for one position.

---

### 5. Risk sizer — [lloyd/risk/sizer.py](lloyd/risk/sizer.py) (new)

**Purpose:** Pure logic: from one `EnsemblePrediction` and current `PortfolioState`, return a `TradeSignal` or `None` (blocked), with quarter-Kelly and all hard limits.

**Imports:** `EnsemblePrediction` from prediction (or common if you re-export), `PortfolioState` from `lloyd.common.models`, `TradeSignal` from `lloyd.execution.base`, `get_settings()` from config. So risk depends on execution only for `TradeSignal`; that’s acceptable (risk produces signals that execution consumes).

**Class:** `RiskSizer` (no async, no DB, no HTTP).

- `**size(self, prediction: EnsemblePrediction, portfolio: PortfolioState) -> TradeSignal | None`**  
  - Resolve category via `get_market_info(conn, ...)` — but sizer is pure logic and has no `conn`. So **category must be passed in**. That implies `TradeSignal` already has `category`, and the caller (main.py) gets category from `get_market_info` and passes it when building the signal. So the sizer receives a prediction and portfolio; for category we need it on the prediction or passed separately. Easiest: **main.py** gets `(platform, platform_id, category)` from `get_market_info`, builds a “candidate” that includes category, and the sizer needs category for the “exposure_by_category” check. So either (1) we add optional `category` to `EnsemblePrediction` in the pipeline (from `market.category`), or (2) main passes category when calling the sizer. Option (2): sizer signature stays `size(prediction, portfolio)` and we need category inside the sizer — so it must be on the prediction or we add a separate argument. **Cleanest:** add an optional `category: str | None` to `EnsemblePrediction` (set in the pipeline from `market.category`) so the sizer stays single-argument for “prediction + portfolio”. Alternatively, have `size(prediction, portfolio, category: str | None)` and main passes category from `get_market_info`. Plan with `**size(prediction, portfolio, category: str | None = None)`** so we don’t need to change the pipeline for category; main gets category from DB and passes it.
- Compute edge, direction, limit_price (final_probability for buy_yes, 1 - final_probability for buy_no in the formula), then:
  - **Edge check:** `abs(prediction.edge) >= settings.min_edge_threshold`; else log INFO (question truncated 80 chars, reason) and return None.
  - **Confidence check:** mean of `p.confidence` for non-None `prediction.model_predictions` >= `settings.LLOYD_MIN_CONFIDENCE`; else block and log.
  - **Kelly:** `_kelly_fraction(edge, price, direction)` then dollar_size = fraction * cash_balance, quantity = dollar_size / price (per PRD formulas). Use `settings.LLOYD_KELLY_FRACTION`.
  - **Position cap (clamp):** if dollar_size > `LLOYD_MAX_POSITION_PCT * cash_balance`, set dollar_size to cap, recompute quantity, log.
  - **Exposure cap:** if `portfolio.total_exposure + dollar_size > LLOYD_MAX_EXPOSURE_PCT * (cash_balance + total_exposure)` then block and log.
  - **Category concentration:** if `category` is not None and `portfolio.exposure_by_category(category) >= 3`, block and log.
  - **Disagreement kill:** `_disagreement_kill(prediction)` — True => block. Implement as: for trade_signal `buy_yes`, block if any model has `probability < 0.20` (i.e. P(NO) > 0.80); for `buy_no`, block if any model has `probability > 0.80`.
  - If all pass, build and return `TradeSignal(market_id=..., ensemble_prediction_id=..., platform=..., platform_id=..., direction=..., quantity=..., limit_price=..., category=...)`. The sizer doesn’t have platform/platform_id; those are filled by the caller. So **sizer returns a “partial” signal (market_id, ensemble_prediction_id, direction, quantity, limit_price, category)** and main (or a small adapter) merges in platform/platform_id from `get_market_info`. Simpler: **have the sizer take (prediction, portfolio, platform, platform_id, category)** so it can return a full `TradeSignal` when it doesn’t block. Then main does one lookup per prediction and passes those in.
- `**_kelly_fraction(self, edge: float, price: float, direction: str) -> float`:** quarter-Kelly per PRD formulas; return the fraction (0..1).
- `**_check_limits(...)`** can return `tuple[bool, str]` (passes, reason) and be used inside `size`; each failure logs at INFO with question and reason.
- `**_disagreement_kill(self, prediction: EnsemblePrediction) -> bool`:** True if any model strongly disagrees with `trade_signal` as above.

**Logging:** Use structlog; on block log at INFO with truncated question and reason. No WARNING for blocked trades.

---

### 6. Paper executor — [lloyd/execution/paper.py](lloyd/execution/paper.py) (new)

**Purpose:** Implement `Executor`: simulate fills (latency, slippage, fees), persist trades and portfolio snapshots.

**Class:** `PaperExecutor(Executor)`.

- **Constructor:** Accepts `conn: sqlite3.Connection` and `settings: Settings`. Also accepts optional `polymarket_client` and `kalshi_client` (e.g. `PolymarketClient | None = None`, `KalshiClient | None = None`). If either client is not provided, instantiate it from config the same way the scanner does and store as an instance attribute. **Do not create new client instances on every `get_current_price` call** — the price-check loop runs across all open positions on a 5-minute cadence; reuse the same two client instances for the lifetime of the executor.
- **async def execute(self, signal: TradeSignal) -> ExecutionResult:**  
  - `await asyncio.sleep(0.3)`.  
  - `executed_price, slippage, fee = _simulate_execution(signal)`.  
  - Insert row into `trades` (status `'open'`, `is_paper=1`, `pnl=NULL`, `opened_at=now` ISO, no `closed_at`). Use `insert_trade` and get `trade_id`.  
  - Return `ExecutionResult(...)`.
- **async def get_current_price(self, market_id: int, platform: str, platform_id: str) -> float | None:**  
  - Use the stored `PolymarketClient` or `KalshiClient` (depending on `platform`) to fetch current price for `platform_id`. Return None on any failure; do not raise.
- `**def is_live(self) -> bool`:** return `False`.
- **def get_portfolio_state(self) -> PortfolioState:**  
  - Query all trades where `status = 'open' AND is_paper = 1`. Then: `total_exposure` = sum(quantity × executed_price) over those open paper trades; **cash_balance** = `LLOYD_PAPER_BANKROLL` − that same sum (only open positions count against the balance). Closed positions (e.g. `status = 'filled'`) do not reduce cash; Stage 4 handles P&L when trades resolve. Build `positions` list of dicts (trade_id, market_id, platform, direction, quantity, entry_price=executed_price, category). Category from join with markets or a column on trades if present.
- **def snapshot_portfolio(self, unrealized_prices: dict | None = None) -> None:**  
  - Get current portfolio state using the same cash/exposure definition as `get_portfolio_state()`: only open paper trades count. If `unrealized_prices` is provided (e.g. from the price-check loop), compute unrealized_pnl and write it; otherwise write unrealized_pnl as NULL. Apply this definition consistently in both `get_portfolio_state()` and `snapshot_portfolio()`.
- `**_simulate_execution(self, signal: TradeSignal) -> tuple[float, float, float]`:**  
  - Slippage: executed_price = limit_price + slippage_direction * (limit_price * LLOYD_PAPER_SLIPPAGE_PCT), +1 for buy_yes, -1 for buy_no.  
  - Fee: `_calculate_fee(platform, quantity, executed_price)`.
- `**_calculate_fee(self, platform: str, quantity: float, price: float) -> float`:** Polymarket: rate * quantity * price; Kalshi: rate * quantity * min(price, 1 - price). Use config for rates.

**DB:** Use the same `conn` passed in; run inserts/updates on that connection. `insert_trade` and a function to write one `portfolio` row (e.g. `insert_portfolio_snapshot(conn, timestamp, cash_balance, total_exposure, unrealized_pnl, realized_pnl, num_open_positions, snapshot)`).

**Note:** `get_open_paper_trades` and `get_market_info` are in db; paper executor or main will call them. Price-check job in main uses `db.get_open_paper_trades(conn)` then loops and calls `executor.get_current_price(...)`.

---

### 7. Live executor stubs — [lloyd/execution/polymarket_live.py](lloyd/execution/polymarket_live.py) and [lloyd/execution/kalshi_live.py](lloyd/execution/kalshi_live.py) (new)

**Purpose:** Implement `Executor` so that every method raises `NotImplementedError` with a clear message; `is_live()` returns `True`.

- **PolymarketLiveExecutor:** `execute` and `get_current_price` raise `NotImplementedError("Polymarket live execution not yet implemented. Set LLOYD_LIVE_TRADING_ENABLED=false.")` (and similar for get_current_price). Module docstring: real implementation would need py-clob-client, EIP-712 signing, wallet private key, CLOB credentials.
- **KalshiLiveExecutor:** Same pattern; docstring: kalshi-python SDK, RSA auth, production base URL.

No shared state; no config needed beyond what’s already there.

---

### 8. main.py wiring — [lloyd/main.py](lloyd/main.py)

**Purpose:** Run risk + execution after the ensemble; add price-check job, health server, and SIGTERM handler.

**1) Executor choice**

- After the ensemble runs, decide executor per prediction: if `settings.live_trading_enabled` (or the chosen attribute name), then for each prediction use `PolymarketLiveExecutor()` or `KalshiLiveExecutor()` based on that prediction’s platform (from `get_market_info`). Otherwise use a single shared `PaperExecutor(conn, settings)` for the whole run.
- So: one `PaperExecutor` instance when paper trading; when live, one executor per platform (or per call) as needed. Create the paper executor once before the loop.

**2) Risk + execution loop**

- After `pipeline.run(results)` you have `predictions: list[EnsemblePrediction]`. For each prediction where `trade_signal != 'no_trade'`:
  - `market_info = get_market_info(conn, prediction.market_id)`; if None, log and continue.
  - `portfolio_state = executor.get_portfolio_state()` (for paper) or equivalent for live.
  - `signal = sizer.size(prediction, portfolio_state, platform=..., platform_id=..., category=...)` with values from market_info. If you chose the signature where sizer returns a full TradeSignal, pass platform, platform_id, category into size and have sizer build the full signal when not blocking.
  - If signal is None, increment blocked count and continue (sizer already logged).
  - `result = await executor.execute(signal)`.
  - Log: trade_placed | platform, direction, quantity, executed_price, fee, slippage, is_paper.
- After the loop: `executor.snapshot_portfolio()` (no prices => unrealized_pnl NULL). Log: stage_3_complete | trades_placed, trades_blocked, cash_balance, total_exposure.

**3) Price-check job**

- New async function `price_check_job()` (or similar): get `open_trades = get_open_paper_trades(conn)` (need to pass conn — use a shared conn or get it from config). For each trade, `current_price = await executor.get_current_price(trade.market_id, trade.platform, trade.platform_id)`. If None, continue. If `abs(current_price - trade.executed_price) >= LLOYD_LARGE_MOVE_THRESHOLD`, call `flag_large_move(conn, trade.id)` and log warning with trade_id, move, entry, current. Then call `executor.snapshot_portfolio(unrealized_prices=...)` with the fetched prices so the snapshot has correct unrealized_pnl. If you don’t pass prices to snapshot_portfolio, you can still call it and leave unrealized_pnl NULL for that run.
- Register with APScheduler: interval `LLOYD_PRICE_CHECK_INTERVAL_MINUTES` minutes. The job must have access to the same executor and conn; use closure or a shared object.

**4) Health check**

- In `_main()` (or wherever the scheduler runs), start a TCP server with `asyncio.start_server(health_handler, "0.0.0.0", settings.LLOYD_HEALTH_CHECK_PORT)`. In `health_handler(reader, writer)`: read until `\r\n\r\n` or a small buffer, check request line contains `GET` and `/health`, then write the 200 JSON response and close the writer. Store the server object for shutdown.

**5) SIGTERM**

- Set a global or context shutdown flag on SIGTERM. In the main loop, after each scan cycle (or at the top of `run_scan_cycle`), check the flag; if set, break out and don’t start another cycle. Use `asyncio.wait_for(run_scan_cycle(), timeout=30)` or run the cycle in a task and wait up to 30s for it to finish when shutting down. Then `scheduler.shutdown(wait=True)`, close the health server, and exit. Register the handler with `signal.signal(signal.SIGTERM, ...)` (and optionally SIGINT for local dev).

---

### 9. Pipeline change for ensemble_prediction_id (required — part of Step 1)

- **lloyd/prediction/ensemble.py:** Add `ensemble_prediction_id: int | None = None` to `EnsemblePrediction`. In the pipeline loop, replace `insert_ensemble_predictions(self._conn, [ep])` with `ep.ensemble_prediction_id = insert_ensemble_prediction(self._conn, ep)`, then append ep to results. Done in the same build step as the db helpers (Step 1) so `ensemble_prediction_id` is always set before results are used for trading.

---

### 10. Tests

- **tests/test_sizer.py:** Use synthetic `EnsemblePrediction` and `PortfolioState` (from common.models). Test: edge below threshold → None; confidence below threshold → None; category at 3 → None, at 2 → pass; total exposure at limit → None, just under → pass; disagreement kill (one model P(YES)>0.80 when signal is buy_no, or P(YES)<0.20 when signal is buy_yes) → None; Kelly math with known edge/price/direction (document expected fraction and quantity in the test); position cap clamp (e.g. Kelly 8%, cap 5% → signal with clamped quantity and log); clean trade → returns TradeSignal with correct direction and quantity. Use a small RiskSizer instance with injected or default settings.
- **tests/test_paper.py:** Mock DB (e.g. in-memory SQLite with init_db) and mock `get_current_price` (or patch the executor’s client calls). Test: execute() returns ExecutionResult with slippage (e.g. limit_price * 1.005 for buy_yes); Polymarket fee = quantity * price * 0.001; Kalshi fee at 0.30, 0.70, 0.50 as in PRD; get_portfolio_state() with multiple open positions aggregates correctly; snapshot_portfolio() writes one row with correct total_exposure and snapshot JSON.
- **tests/test_executor_interface.py:** Assert PaperExecutor, PolymarketLiveExecutor, KalshiLiveExecutor are subclasses of Executor; assert each implements execute, get_current_price, is_live; assert is_live() False for paper, True for stubs; assert calling execute() or get_current_price() on live stubs raises NotImplementedError.

---

## Order of implementation (summary)

1. **db.py** — schema (trades, portfolio), Trade dataclass, **insert_ensemble_prediction**, get_market_info, get_open_paper_trades, flag_large_move, insert_trade, insert_portfolio_snapshot. **prediction/ensemble.py** — add `ensemble_prediction_id` to `EnsemblePrediction`, replace batch insert with single-insert and set `ep.ensemble_prediction_id` before appending to results.
2. **config.py** — Stage 3 env vars.
3. **common/models.py** — PortfolioState (+ exposure_by_category).
4. **execution/base.py** — TradeSignal (ensemble_prediction_id: int), ExecutionResult, Executor ABC.
5. **risk/sizer.py** — RiskSizer.size, _kelly_fraction, _check_limits, _disagreement_kill (category passed in from main).
6. **execution/paper.py** — PaperExecutor: __init__(conn, settings, polymarket_client=None, kalshi_client=None); instantiate clients from config when not provided and reuse; execute, get_current_price (using stored clients), get_portfolio_state (cash_balance = bankroll − sum over open paper trades only), snapshot_portfolio(unrealized_prices=...), _simulate_execution, _calculate_fee.
7. **execution/polymarket_live.py** and **kalshi_live.py** — stubs.
8. **main.py** — executor factory, risk/execution loop, price-check job, health server, SIGTERM handler.
9. **Tests** — test_sizer.py, test_paper.py, test_executor_interface.py.

---

## Resolved

- **trades.platform_id:** Have `get_open_paper_trades` join `markets` and return `platform_id` in the Trade dataclass so the price-check job can call `get_current_price` without changing the table schema.
- **Cash balance:** **cash_balance = LLOYD_PAPER_BANKROLL − sum(quantity × executed_price)** for trades where **status = 'open' AND is_paper = 1**. Only open positions count against the balance. Closed positions (e.g. status = 'filled') do not reduce cash; Stage 4 will handle P&L accounting when trades resolve. Apply in both `get_portfolio_state()` and `snapshot_portfolio()`.
- **Snapshot after execute:** Call `snapshot_portfolio()` after every execute (unrealized_pnl NULL) and after every price-check cycle (pass unrealized_prices when available).
