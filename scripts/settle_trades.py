"""Manually settle one or more paper trades with a confirmed outcome.

Usage:
    railway run python scripts/settle_trades.py --trade-id 26 --outcome no
    railway run python scripts/settle_trades.py --trade-ids 26,29,31 --outcome no

Or locally:
    LLOYD_DATABASE_PATH=./lloyd.db python scripts/settle_trades.py --trade-id 26 --outcome no
"""
from __future__ import annotations

import argparse
import os
import sqlite3
from datetime import datetime, timezone

DB_PATH = os.getenv("LLOYD_DATABASE_PATH", "/data/lloyd.db")


def _calc_pnl(direction: str, quantity: float, executed_price: float, fee: float, outcome: str) -> float:
    if outcome == "void":
        return 0.0
    if direction == "buy_yes":
        if outcome == "yes":
            return (1.0 - executed_price) * quantity - fee
        return -executed_price * quantity - fee
    if outcome == "no":
        return (1.0 - executed_price) * quantity - fee
    return -executed_price * quantity - fee


def settle_trade(conn: sqlite3.Connection, trade_id: int, outcome: str) -> bool:
    trade = conn.execute(
        """SELECT t.id, t.market_id, t.status, t.direction, t.quantity,
                  t.executed_price, t.fee, m.platform
           FROM trades t
           JOIN markets m ON m.id = t.market_id
           WHERE t.id = ?""",
        (trade_id,),
    ).fetchone()

    if trade is None:
        print(f"  trade_id={trade_id}: NOT FOUND")
        return False

    if trade[2] != "open":
        print(f"  trade_id={trade_id}: already {trade[2]} — skip")
        return False

    market_id = trade[1]
    platform = trade[7]
    now = datetime.now(timezone.utc).isoformat()

    existing = conn.execute(
        "SELECT id FROM outcomes WHERE market_id = ?", (market_id,)
    ).fetchone()
    if not existing:
        conn.execute(
            "INSERT INTO outcomes (market_id, platform, outcome, resolved_at) VALUES (?, ?, ?, ?)",
            (market_id, platform, outcome, now),
        )

    pnl = _calc_pnl(trade[3], trade[4], trade[5], trade[6], outcome)
    conn.execute(
        "UPDATE trades SET status = 'settled', pnl = ?, closed_at = ? WHERE id = ?",
        (pnl, now, trade_id),
    )
    conn.commit()
    print(f"  trade_id={trade_id}: settled outcome={outcome} pnl={pnl:.4f}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Manually settle Lloyd paper trades")
    parser.add_argument("--trade-id", type=int, help="Single trade ID")
    parser.add_argument("--trade-ids", type=str, help="Comma-separated trade IDs")
    parser.add_argument("--outcome", required=True, choices=["yes", "no", "void"])
    args = parser.parse_args()

    ids: list[int] = []
    if args.trade_id is not None:
        ids.append(args.trade_id)
    if args.trade_ids:
        ids.extend(int(x.strip()) for x in args.trade_ids.split(",") if x.strip())

    if not ids:
        parser.error("Provide --trade-id or --trade-ids")

    print(f"DB: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    settled = sum(settle_trade(conn, tid, args.outcome) for tid in ids)
    conn.close()
    print(f"\nSettled {settled}/{len(ids)} trade(s).")
    return 0 if settled == len(ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
