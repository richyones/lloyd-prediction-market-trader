"""List open paper trades past close_date (resolver / healthcheck backlog)."""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone

DB_PATH = os.getenv("LLOYD_DATABASE_PATH", "/data/lloyd.db")
LOOKBACK_DAYS = int(os.getenv("RESOLVER_LOOKBACK_DAYS", "3"))


def main() -> int:
    print(f"Connecting to DB: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cutoff = datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)

    rows = conn.execute(
        """
        SELECT t.id AS trade_id, t.direction, t.status, t.opened_at,
               m.platform, m.platform_id, m.question, m.close_date,
               m.current_price AS db_yes_price
        FROM trades t
        JOIN markets m ON m.id = t.market_id
        WHERE t.status = 'open' AND t.is_paper = 1
        ORDER BY m.close_date ASC, t.id ASC
        """
    ).fetchall()

    if not rows:
        print("No open paper trades.")
        return 0

    overdue = []
    for row in rows:
        close_raw = row["close_date"]
        if not close_raw:
            continue
        try:
            close_dt = datetime.fromisoformat(str(close_raw).replace("Z", "+00:00"))
            if close_dt.tzinfo is None:
                close_dt = close_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if close_dt < cutoff:
            overdue.append(row)

    print(f"\nOpen trades past {LOOKBACK_DAYS}d lookback ({cutoff.isoformat()}):\n")
    if not overdue:
        print("  (none overdue by healthcheck threshold)")
    else:
        for row in overdue:
            q = (row["question"] or "")[:70]
            print(
                f"  trade_id={row['trade_id']:>3}  {row['platform']:<10}  "
                f"close={row['close_date']}  db_yes={row['db_yes_price']}  "
                f"{row['direction']:<8}  {q}"
            )
        print(f"\nTotal overdue: {len(overdue)}")

    print(f"\nAll open paper trades: {len(rows)}")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
