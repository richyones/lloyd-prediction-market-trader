from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import structlog
from pydantic import ValidationError

from lloyd.common.models import Market, MarketPair, ScanResult

log = structlog.get_logger()


@dataclass
class Trade:
    """Lightweight DTO for open trade rows returned by ``get_open_paper_trades``."""

    id: int
    market_id: int
    platform: str
    platform_id: str
    direction: str
    quantity: float
    executed_price: float
    category: str | None = None

SCHEMA = """\
CREATE TABLE IF NOT EXISTS markets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    question TEXT NOT NULL,
    category TEXT,
    current_price REAL,
    volume REAL,
    liquidity REAL,
    open_interest REAL,
    close_date TEXT,
    raw_data TEXT,
    fetched_at TEXT NOT NULL,
    UNIQUE(platform, platform_id, fetched_at)
);

CREATE TABLE IF NOT EXISTS market_pairs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    polymarket_id TEXT,
    kalshi_id TEXT,
    similarity_score REAL,
    price_divergence REAL,
    matched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS scan_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    exploitability_score REAL,
    scan_timestamp TEXT NOT NULL,
    passed_filter INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS research_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    query_hash TEXT NOT NULL,
    articles TEXT NOT NULL,
    context_quality TEXT NOT NULL,
    article_count INTEGER DEFAULT 0,
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    UNIQUE(market_id, query_hash)
);

CREATE TABLE IF NOT EXISTS predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    model_name TEXT NOT NULL,
    probability REAL NOT NULL,
    confidence INTEGER,
    reasoning TEXT,
    evidence_for TEXT,
    evidence_against TEXT,
    market_disagree_reason TEXT,
    tokens_used INTEGER,
    cost_usd REAL,
    prompt_version TEXT NOT NULL,
    context_quality TEXT NOT NULL,
    input_context_hash TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ensemble_predictions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    ensemble_probability REAL NOT NULL,
    market_price REAL NOT NULL,
    edge REAL NOT NULL,
    alpha REAL DEFAULT 0.3,
    final_probability REAL NOT NULL,
    model_predictions TEXT NOT NULL,
    trade_signal TEXT NOT NULL,
    tier2_used INTEGER DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    ensemble_prediction_id INTEGER NOT NULL REFERENCES ensemble_predictions(id),
    platform TEXT NOT NULL,
    direction TEXT NOT NULL,
    quantity REAL NOT NULL,
    limit_price REAL,
    executed_price REAL,
    slippage REAL,
    fee REAL,
    is_paper INTEGER DEFAULT 1,
    status TEXT DEFAULT 'open',
    large_move_flagged INTEGER DEFAULT 0,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    pnl REAL
);

CREATE TABLE IF NOT EXISTS portfolio (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL,
    cash_balance REAL NOT NULL,
    total_exposure REAL NOT NULL,
    unrealized_pnl REAL,
    realized_pnl REAL,
    num_open_positions INTEGER,
    snapshot TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    market_id INTEGER REFERENCES markets(id),
    platform TEXT NOT NULL,
    outcome TEXT NOT NULL,
    resolved_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS model_scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model_name TEXT NOT NULL,
    category TEXT NOT NULL DEFAULT '',
    period_type TEXT NOT NULL DEFAULT 'alltime',
    brier_score REAL NOT NULL,
    calibration_error REAL,
    num_predictions INTEGER,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    UNIQUE(model_name, category, period_type, period_end)
);
"""


def get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    conn.commit()


def insert_markets(conn: sqlite3.Connection, markets: list[Market]) -> None:
    conn.executemany(
        """INSERT OR IGNORE INTO markets
           (platform, platform_id, question, category, current_price,
            volume, liquidity, open_interest, close_date, raw_data, fetched_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                m.platform,
                m.platform_id,
                m.question,
                m.category,
                m.current_price,
                m.volume,
                m.liquidity,
                m.open_interest,
                m.close_date.isoformat() if m.close_date else None,
                json.dumps(m.raw_data),
                m.fetched_at.isoformat(),
            )
            for m in markets
        ],
    )
    conn.commit()


def get_latest_markets(conn: sqlite3.Connection) -> list[Market]:
    """Latest snapshot per (platform, platform_id) from ``markets`` for matcher input."""
    rows = conn.execute(
        """
        SELECT platform, platform_id, question, category, current_price,
               volume, liquidity, open_interest, close_date, raw_data, fetched_at
        FROM markets
        WHERE fetched_at = (
            SELECT MAX(m2.fetched_at)
            FROM markets m2
            WHERE m2.platform = markets.platform
              AND m2.platform_id = markets.platform_id
        )
        """
    ).fetchall()

    out: list[Market] = []
    for row in rows:
        (
            platform,
            platform_id,
            question,
            category,
            current_price,
            volume,
            liquidity,
            open_interest,
            close_date_raw,
            _raw_data_col,
            fetched_at_raw,
        ) = row
        close_date: datetime | None = None
        if close_date_raw is not None:
            close_date = datetime.fromisoformat(close_date_raw)
        try:
            fetched_at = datetime.fromisoformat(fetched_at_raw)
        except (TypeError, ValueError):
            log.debug(
                "get_latest_markets_skip_row",
                platform=platform,
                platform_id=platform_id,
                error="invalid_fetched_at",
            )
            continue
        try:
            out.append(
                Market(
                    platform=platform,
                    platform_id=platform_id,
                    question=question,
                    category=category,
                    current_price=current_price,
                    volume=volume,
                    liquidity=liquidity,
                    open_interest=open_interest,
                    close_date=close_date,
                    raw_data={},
                    fetched_at=fetched_at,
                )
            )
        except ValidationError as exc:
            log.debug(
                "get_latest_markets_skip_row",
                platform=platform,
                platform_id=platform_id,
                error=str(exc),
            )
    return out


def _resolve_market_id(
    conn: sqlite3.Connection,
    market: Market,
) -> int | None:
    """Look up the DB row id for a market by its natural key."""
    row = conn.execute(
        "SELECT id FROM markets WHERE platform = ? AND platform_id = ? AND fetched_at = ?",
        (market.platform, market.platform_id, market.fetched_at.isoformat()),
    ).fetchone()
    return row[0] if row else None


def insert_scan_results(
    conn: sqlite3.Connection,
    results: list[ScanResult],
) -> None:
    rows: list[tuple[int, float, str, int]] = []
    for r in results:
        market_id = _resolve_market_id(conn, r.market)
        if market_id is None:
            continue
        rows.append((
            market_id,
            r.exploitability_score,
            r.scan_timestamp.isoformat(),
            int(r.passed_filter),
        ))
    conn.executemany(
        """INSERT INTO scan_results
           (market_id, exploitability_score, scan_timestamp, passed_filter)
           VALUES (?, ?, ?, ?)""",
        rows,
    )
    conn.commit()


def insert_market_pairs(
    conn: sqlite3.Connection,
    pairs: list[MarketPair],
) -> None:
    conn.executemany(
        """INSERT INTO market_pairs
           (polymarket_id, kalshi_id, similarity_score, price_divergence, matched_at)
           VALUES (?, ?, ?, ?, ?)""",
        [
            (
                p.polymarket_market.platform_id,
                p.kalshi_market.platform_id,
                p.similarity_score,
                p.price_divergence,
                p.matched_at.isoformat(),
            )
            for p in pairs
        ],
    )
    conn.commit()


def get_market_id(conn: sqlite3.Connection, market: Market) -> int | None:
    """Public wrapper around ``_resolve_market_id``."""
    return _resolve_market_id(conn, market)


def insert_predictions(
    conn: sqlite3.Connection,
    predictions: list,
    market_id: int,
) -> None:
    """Persist a list of ``PredictionResult`` objects for a single market."""
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """INSERT INTO predictions
           (market_id, model_name, probability, confidence, reasoning,
            evidence_for, evidence_against, market_disagree_reason,
            tokens_used, cost_usd, prompt_version, context_quality,
            input_context_hash, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                market_id,
                p.model_name,
                p.probability,
                p.confidence,
                p.reasoning,
                p.evidence_for,
                p.evidence_against,
                p.market_disagree_reason,
                p.tokens_used,
                p.cost_usd,
                p.prompt_version,
                p.context_quality,
                p.input_context_hash,
                now,
            )
            for p in predictions
        ],
    )
    conn.commit()


def insert_ensemble_predictions(
    conn: sqlite3.Connection,
    ensemble_list: list,
) -> None:
    """Persist a list of ``EnsemblePrediction`` objects."""
    now = datetime.now(timezone.utc).isoformat()
    conn.executemany(
        """INSERT INTO ensemble_predictions
           (market_id, ensemble_probability, market_price, edge, alpha,
            final_probability, model_predictions, trade_signal, tier2_used,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                ep.market_id,
                ep.ensemble_probability,
                ep.market_price,
                ep.edge,
                ep.alpha,
                ep.final_probability,
                json.dumps([p.model_dump() for p in ep.model_predictions]),
                ep.trade_signal,
                int(ep.tier2_used),
                now,
            )
            for ep in ensemble_list
        ],
    )
    conn.commit()


# --- Stage 3 helpers ---


def insert_ensemble_prediction(conn: sqlite3.Connection, ep: object) -> int:
    """Insert a single ``EnsemblePrediction`` and return its row id."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO ensemble_predictions
           (market_id, ensemble_probability, market_price, edge, alpha,
            final_probability, model_predictions, trade_signal, tier2_used,
            created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            ep.market_id,
            ep.ensemble_probability,
            ep.market_price,
            ep.edge,
            ep.alpha,
            ep.final_probability,
            json.dumps([p.model_dump() for p in ep.model_predictions]),
            ep.trade_signal,
            int(ep.tier2_used),
            now,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def get_market_info(
    conn: sqlite3.Connection, market_id: int
) -> tuple[str, str, str | None] | None:
    """Return ``(platform, platform_id, category)`` for a market row."""
    row = conn.execute(
        "SELECT platform, platform_id, category FROM markets WHERE id = ?",
        (market_id,),
    ).fetchone()
    if row is None:
        return None
    return (row[0], row[1], row[2])


def get_open_paper_trades(conn: sqlite3.Connection) -> list[Trade]:
    """Return all open paper trades, joining markets for platform_id and category."""
    rows = conn.execute(
        """SELECT t.id, t.market_id, t.platform, m.platform_id,
                  t.direction, t.quantity, t.executed_price, m.category
           FROM trades t
           JOIN markets m ON m.id = t.market_id
           WHERE t.status = 'open' AND t.is_paper = 1""",
    ).fetchall()
    return [
        Trade(
            id=r[0],
            market_id=r[1],
            platform=r[2],
            platform_id=r[3],
            direction=r[4],
            quantity=r[5],
            executed_price=r[6],
            category=r[7],
        )
        for r in rows
    ]


def get_recent_scan_results(
    conn: sqlite3.Connection, limit: int,
) -> list[ScanResult]:
    """Return the most recent cycle's scan results by exploitability score."""
    rows = conn.execute(
        """SELECT m.platform, m.platform_id, m.question, m.category,
                  m.current_price, m.volume, m.liquidity, m.open_interest,
                  m.close_date, m.raw_data, m.fetched_at,
                  sr.exploitability_score, sr.scan_timestamp, sr.passed_filter
           FROM scan_results sr
           JOIN markets m ON m.id = sr.market_id
           WHERE sr.scan_timestamp = (
               SELECT MAX(scan_timestamp) FROM scan_results
           )
           ORDER BY sr.exploitability_score DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()

    results: list[ScanResult] = []
    for r in rows:
        close_date = datetime.fromisoformat(r[8]) if r[8] else None
        raw_data = json.loads(r[9]) if r[9] else {}
        market = Market(
            platform=r[0],
            platform_id=r[1],
            question=r[2],
            category=r[3],
            current_price=r[4],
            volume=r[5],
            liquidity=r[6],
            open_interest=r[7],
            close_date=close_date,
            raw_data=raw_data,
            fetched_at=datetime.fromisoformat(r[10]),
        )
        results.append(ScanResult(
            market=market,
            exploitability_score=r[11],
            passed_filter=bool(r[13]),
            scan_timestamp=datetime.fromisoformat(r[12]),
        ))
    return results


def flag_large_move(conn: sqlite3.Connection, trade_id: int) -> None:
    """Mark a trade as having experienced a large price move."""
    conn.execute(
        "UPDATE trades SET large_move_flagged = 1 WHERE id = ?",
        (trade_id,),
    )
    conn.commit()


def insert_trade(
    conn: sqlite3.Connection,
    *,
    market_id: int,
    ensemble_prediction_id: int,
    platform: str,
    direction: str,
    quantity: float,
    limit_price: float,
    executed_price: float,
    slippage: float,
    fee: float,
    is_paper: bool,
    status: str,
    opened_at: str,
) -> int:
    """Insert a single trade row and return the new row id."""
    cur = conn.execute(
        """INSERT INTO trades
           (market_id, ensemble_prediction_id, platform, direction, quantity,
            limit_price, executed_price, slippage, fee, is_paper, status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            market_id,
            ensemble_prediction_id,
            platform,
            direction,
            quantity,
            limit_price,
            executed_price,
            slippage,
            fee,
            int(is_paper),
            status,
            opened_at,
        ),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def insert_portfolio_snapshot(
    conn: sqlite3.Connection,
    *,
    timestamp: str,
    cash_balance: float,
    total_exposure: float,
    unrealized_pnl: float | None,
    realized_pnl: float | None,
    num_open_positions: int,
    snapshot: str,
) -> None:
    """Write one portfolio snapshot row."""
    conn.execute(
        """INSERT INTO portfolio
           (timestamp, cash_balance, total_exposure, unrealized_pnl,
            realized_pnl, num_open_positions, snapshot)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            timestamp,
            cash_balance,
            total_exposure,
            unrealized_pnl,
            realized_pnl,
            num_open_positions,
            snapshot,
        ),
    )
    conn.commit()
