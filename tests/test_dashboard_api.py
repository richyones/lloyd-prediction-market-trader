from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from lloyd.config import Settings
from lloyd.db import init_db
from lloyd.main import _build_api_data


def test_api_data_includes_settled_trades(tmp_path, monkeypatch):
    db_path = tmp_path / "dashboard.db"
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)

    now = datetime.now(timezone.utc).isoformat()
    market_id = conn.execute(
        """INSERT INTO markets (platform, platform_id, question, current_price, volume, fetched_at)
           VALUES ('polymarket', 'pm-dashboard', 'Dashboard test market', 0.55, 1000, ?)""",
        (now,),
    ).lastrowid

    ep_id = conn.execute(
        """INSERT INTO ensemble_predictions
           (market_id, ensemble_probability, market_price, edge, final_probability,
            model_predictions, trade_signal, created_at)
           VALUES (?, 0.6, 0.55, 0.05, 0.58, '[]', 'buy_yes', ?)""",
        (market_id, now),
    ).lastrowid

    conn.execute(
        """INSERT INTO trades
           (market_id, ensemble_prediction_id, platform, direction, quantity, limit_price,
            executed_price, slippage, fee, is_paper, status, opened_at, closed_at, pnl)
           VALUES (?, ?, 'polymarket', 'buy_yes', 10, 0.55, 0.55, 0.0, 0.01, 1, 'settled', ?, ?, 1.23)""",
        (market_id, ep_id, now, now),
    )
    conn.execute(
        "INSERT INTO outcomes (market_id, platform, outcome, resolved_at) VALUES (?, 'polymarket', 'yes', ?)",
        (market_id, now),
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(
        "lloyd.main.get_settings",
        lambda: Settings(database_path=str(db_path)),
    )

    payload = json.loads(_build_api_data())
    assert len(payload["settled_trades"]) == 1
    assert payload["settled_trades"][0]["platform"] == "polymarket"
    assert payload["settled_trades"][0]["outcome"] == "yes"
