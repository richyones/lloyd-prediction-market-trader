"""One-off: settle Jun 2026 Kalshi trades that resolved NO (~$0.02 live price in logs).

Run on Railway after deploy (or if resolver overrides not yet applied):
    railway run python scripts/settle_jun2026_obvious_no.py

Idempotent — skips already-settled trades.
"""
from __future__ import annotations

import subprocess
import sys

# From Railway logs 2026-06-06: large_move_flagged current=0.02
OBVIOUS_NO_TRADE_IDS = [26, 29, 31, 33, 35, 38, 39]


def main() -> int:
    ids = ",".join(str(i) for i in OBVIOUS_NO_TRADE_IDS)
    cmd = [
        sys.executable,
        "scripts/settle_trades.py",
        "--trade-ids",
        ids,
        "--outcome",
        "no",
    ]
    print("Running:", " ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
