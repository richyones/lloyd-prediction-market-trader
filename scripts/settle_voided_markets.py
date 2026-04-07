from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

CLOB_BASE_URL = "https://clob.polymarket.com"
DEFAULT_DB_PATH = "/data/lloyd.db"
TARGET_PLATFORM_IDS = [
    "0x814657a16a3c5b39834864251372e30f68ddcd0f040c5c6a83a52cddb2c35226",
    "0xbfe2252feb42b566915b9ad599ded119a0dcefd318734a8a691fe2add291edc6",
    "0x3d9534e655c68bd1811bdb721704d7434c6ce214a69043e9e2c4a13c25db33f5",
    "0x337e5c84b83679d5557acafcf78b5c5d4d932b9fcd7dfba7966249d64f0f0a0f",
    "0xa30f27c017bbf082c4873d313151203a1ac16989495dfeec039b8ee5243ec7e7",
    "0xf1050a8b0233348a95d75083825b1dd360e332557b15517a89fb4b0f05708ad2",
    "0xdb6830a620e3a3bfdf0677d98b0628f041e52e2caff76d76d3567578364d356f",
]


@dataclass
class SettlementSummary:
    markets_checked: int = 0
    markets_settled: int = 0
    trades_settled: int = 0
    still_open: int = 0
    closed_no_winner: int = 0
    no_open_trades: int = 0
    api_errors: int = 0
    db_errors: int = 0


def _get_connection(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _record_outcome(conn: sqlite3.Connection, market_id: int, outcome: str) -> bool:
    existing = conn.execute(
        "SELECT id FROM outcomes WHERE market_id = ?",
        (market_id,),
    ).fetchone()
    if existing:
        return False

    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO outcomes (market_id, platform, outcome, resolved_at) VALUES (?, ?, ?, ?)",
        (market_id, "polymarket", outcome, now),
    )
    conn.commit()
    return True


def _calculate_pnl(
    direction: str,
    quantity: float,
    executed_price: float,
    fee: float,
    outcome: str,
) -> float:
    if outcome == "void":
        return 0.0

    if direction == "buy_yes":
        if outcome == "yes":
            return (1.0 - executed_price) * quantity - fee
        return -executed_price * quantity - fee

    if outcome == "no":
        return (1.0 - executed_price) * quantity - fee
    return -executed_price * quantity - fee


def _settle_trades(conn: sqlite3.Connection, market_id: int, outcome: str) -> int:
    rows = conn.execute(
        """SELECT id, direction, quantity, executed_price, fee
           FROM trades
           WHERE market_id = ? AND status = 'open'""",
        (market_id,),
    ).fetchall()
    if not rows:
        return 0

    now = datetime.now(timezone.utc).isoformat()
    count = 0
    for trade_id, direction, quantity, executed_price, fee in rows:
        pnl = _calculate_pnl(direction, quantity, executed_price, fee, outcome)
        conn.execute(
            "UPDATE trades SET status = 'settled', pnl = ?, closed_at = ? WHERE id = ?",
            (pnl, now, trade_id),
        )
        count += 1
    conn.commit()
    return count


def _get_target_markets(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    placeholders = ",".join(["?"] * len(TARGET_PLATFORM_IDS))
    query = f"""
        SELECT DISTINCT t.market_id, m.platform_id
        FROM trades t
        JOIN markets m ON m.id = t.market_id
        WHERE t.status = 'open'
          AND m.platform = 'polymarket'
          AND m.platform_id IN ({placeholders})
    """
    rows = conn.execute(query, TARGET_PLATFORM_IDS).fetchall()
    return [(int(row[0]), str(row[1])) for row in rows]


def _extract_outcome(clob_market: dict) -> str | None:
    if not clob_market.get("closed"):
        return None

    tokens = clob_market.get("tokens") or []
    for token in tokens:
        if isinstance(token, dict) and token.get("winner") is True:
            outcome = str(token.get("outcome", "")).lower()
            if outcome in ("yes", "no"):
                return outcome
            return "void"
    return ""


def main() -> int:
    db_path = os.getenv("LLOYD_DATABASE_PATH", DEFAULT_DB_PATH)
    summary = SettlementSummary()

    try:
        conn = _get_connection(db_path)
    except sqlite3.Error as exc:
        print(f"failed to connect to database at {db_path}: {exc}")
        return 1

    try:
        targets = _get_target_markets(conn)
        if not targets:
            print("no matching open polymarket trades found for target platform IDs")
            return 0

        with httpx.Client(timeout=30.0) as client:
            for market_id, platform_id in targets:
                summary.markets_checked += 1
                try:
                    resp = client.get(f"{CLOB_BASE_URL}/markets/{platform_id}")
                    resp.raise_for_status()
                except Exception as exc:
                    summary.api_errors += 1
                    print(f"[error] market_id={market_id} platform_id={platform_id} api_error={exc}")
                    continue

                outcome = _extract_outcome(resp.json())
                if outcome is None:
                    summary.still_open += 1
                    print(f"[skip-open] market_id={market_id} platform_id={platform_id}")
                    continue
                if outcome == "":
                    summary.closed_no_winner += 1
                    print(f"[skip-no-winner] market_id={market_id} platform_id={platform_id}")
                    continue

                try:
                    _record_outcome(conn, market_id, outcome)
                    settled = _settle_trades(conn, market_id, outcome)
                except sqlite3.Error as exc:
                    summary.db_errors += 1
                    print(f"[error] market_id={market_id} platform_id={platform_id} db_error={exc}")
                    continue

                if settled == 0:
                    summary.no_open_trades += 1
                    print(f"[skip-no-open-trades] market_id={market_id} platform_id={platform_id}")
                    continue

                summary.markets_settled += 1
                summary.trades_settled += settled
                print(
                    f"[settled] market_id={market_id} platform_id={platform_id} "
                    f"outcome={outcome} trades={settled}"
                )
    finally:
        conn.close()

    print("")
    print("manual settlement summary")
    print(f"db_path: {db_path}")
    print(f"markets_checked: {summary.markets_checked}")
    print(f"markets_settled: {summary.markets_settled}")
    print(f"trades_settled: {summary.trades_settled}")
    print(f"still_open: {summary.still_open}")
    print(f"closed_no_winner: {summary.closed_no_winner}")
    print(f"no_open_trades: {summary.no_open_trades}")
    print(f"api_errors: {summary.api_errors}")
    print(f"db_errors: {summary.db_errors}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
