"""Tests for lloyd.postmortem.resolver — outcome resolution and P&L settlement."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from lloyd.config import Settings
from lloyd.db import init_db
from lloyd.postmortem.resolver import OutcomeResolver, ResolverResult


@pytest.fixture()
def db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.execute("PRAGMA foreign_keys=ON")
    init_db(conn)
    yield conn
    conn.close()


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        database_path=":memory:",
        kalshi_base_url="https://demo-api.kalshi.co/trade-api/v2",
        kalshi_api_key_id="",
        kalshi_rsa_key_path="",
    )


def _seed_market(conn: sqlite3.Connection, platform: str, platform_id: str, market_id: int | None = None) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO markets (platform, platform_id, question, current_price, volume, fetched_at)
           VALUES (?, ?, 'Test market', 0.60, 10000, ?)""",
        (platform, platform_id, now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _seed_ensemble(conn: sqlite3.Connection, market_id: int) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO ensemble_predictions
           (market_id, ensemble_probability, market_price, edge, final_probability,
            model_predictions, trade_signal, created_at)
           VALUES (?, 0.65, 0.60, 0.05, 0.62, '[]', 'buy_yes', ?)""",
        (market_id, now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


def _seed_trade(
    conn: sqlite3.Connection,
    market_id: int,
    ep_id: int,
    platform: str,
    direction: str = "buy_yes",
    quantity: float = 10.0,
    executed_price: float = 0.60,
    fee: float = 0.006,
) -> int:
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        """INSERT INTO trades
           (market_id, ensemble_prediction_id, platform, direction, quantity,
            limit_price, executed_price, slippage, fee, is_paper, status, opened_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 0.0, ?, 1, 'open', ?)""",
        (market_id, ep_id, platform, direction, quantity, executed_price, executed_price, fee, now),
    )
    conn.commit()
    return cur.lastrowid  # type: ignore[return-value]


class TestCalculatePnl:
    """Verify P&L formulas for all direction/outcome combos."""

    def test_buy_yes_wins(self):
        # buy_yes + yes: (1.0 - 0.60) * 10 - 0.006 = 3.994
        pnl = OutcomeResolver._calculate_pnl("buy_yes", 10.0, 0.60, 0.006, "yes")
        assert abs(pnl - 3.994) < 1e-9

    def test_buy_yes_loses(self):
        # buy_yes + no: -0.60 * 10 - 0.006 = -6.006
        pnl = OutcomeResolver._calculate_pnl("buy_yes", 10.0, 0.60, 0.006, "no")
        assert abs(pnl - (-6.006)) < 1e-9

    def test_buy_no_wins(self):
        # buy_no + no: (1.0 - 0.40) * 10 - 0.006 = 5.994
        # executed_price = 0.40 (the NO share price)
        pnl = OutcomeResolver._calculate_pnl("buy_no", 10.0, 0.40, 0.006, "no")
        assert abs(pnl - 5.994) < 1e-9

    def test_buy_no_loses(self):
        # buy_no + yes: -0.40 * 10 - 0.006 = -4.006
        pnl = OutcomeResolver._calculate_pnl("buy_no", 10.0, 0.40, 0.006, "yes")
        assert abs(pnl - (-4.006)) < 1e-9

    def test_void_returns_zero(self):
        pnl = OutcomeResolver._calculate_pnl("buy_yes", 10.0, 0.60, 0.006, "void")
        assert pnl == 0.0


class TestRecordOutcome:
    def test_idempotent(self, db, settings):
        mid = _seed_market(db, "polymarket", "pm-test")
        resolver = OutcomeResolver(db, settings)

        first = resolver._record_outcome(mid, "polymarket", "yes")
        assert first is True

        second = resolver._record_outcome(mid, "polymarket", "yes")
        assert second is False

        rows = db.execute("SELECT COUNT(*) FROM outcomes WHERE market_id = ?", (mid,)).fetchone()
        assert rows[0] == 1


class TestSettleTrades:
    def test_settles_open_trades(self, db, settings):
        mid = _seed_market(db, "polymarket", "pm-settle")
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "polymarket", "buy_yes", 10.0, 0.60, 0.006)

        resolver = OutcomeResolver(db, settings)
        count = resolver._settle_trades(mid, "yes")

        assert count == 1
        row = db.execute("SELECT status, pnl FROM trades WHERE market_id = ?", (mid,)).fetchone()
        assert row[0] == "settled"
        assert abs(row[1] - 3.994) < 1e-9

    def test_already_settled_not_double_counted(self, db, settings):
        mid = _seed_market(db, "polymarket", "pm-double")
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "polymarket", "buy_yes", 10.0, 0.60, 0.006)

        resolver = OutcomeResolver(db, settings)
        resolver._settle_trades(mid, "yes")
        count2 = resolver._settle_trades(mid, "yes")
        assert count2 == 0


class TestMarketsResolvedCount:
    """Verify markets_resolved only increments on new outcome rows."""

    def test_does_not_overcount_on_repeat(self, db, settings):
        mid = _seed_market(db, "polymarket", "pm-count")
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "polymarket")

        resolver = OutcomeResolver(db, settings)

        # Simulate: record outcome + settle
        new = resolver._record_outcome(mid, "polymarket", "yes")
        assert new is True

        # Second call: should not be new
        new2 = resolver._record_outcome(mid, "polymarket", "yes")
        assert new2 is False


@pytest.mark.asyncio
class TestRunWithMocks:
    async def test_polymarket_failure_does_not_block_kalshi(self, db, settings):
        pm_mid = _seed_market(db, "polymarket", "pm-fail")
        pm_ep = _seed_ensemble(db, pm_mid)
        _seed_trade(db, pm_mid, pm_ep, "polymarket")

        ka_mid = _seed_market(db, "kalshi", "ka-ok")
        ka_ep = _seed_ensemble(db, ka_mid)
        _seed_trade(db, ka_mid, ka_ep, "kalshi")

        resolver = OutcomeResolver(db, settings)

        with patch.object(
            resolver, "_fetch_polymarket_resolutions", new_callable=AsyncMock,
            side_effect=Exception("PM down"),
        ), patch.object(
            resolver, "_fetch_kalshi_resolutions", new_callable=AsyncMock,
        ):
            result = await resolver.run()

        assert len(result.errors) >= 1
        assert "polymarket" in result.errors[0].lower()

    async def test_kalshi_failure_does_not_block_polymarket(self, db, settings):
        pm_mid = _seed_market(db, "polymarket", "pm-ok")
        pm_ep = _seed_ensemble(db, pm_mid)
        _seed_trade(db, pm_mid, pm_ep, "polymarket")

        ka_mid = _seed_market(db, "kalshi", "ka-fail")
        ka_ep = _seed_ensemble(db, ka_mid)
        _seed_trade(db, ka_mid, ka_ep, "kalshi")

        resolver = OutcomeResolver(db, settings)

        with patch.object(
            resolver, "_fetch_kalshi_resolutions", new_callable=AsyncMock,
            side_effect=Exception("Kalshi down"),
        ), patch.object(
            resolver, "_fetch_polymarket_resolutions", new_callable=AsyncMock,
        ):
            result = await resolver.run()

        assert len(result.errors) >= 1
        assert "kalshi" in result.errors[0].lower()


@pytest.mark.asyncio
class TestPolymarketResolutionFetch:
    async def test_422_one_market_does_not_block_other_market(self, db, settings):
        bad_mid = _seed_market(db, "polymarket", "pm-422")
        bad_ep = _seed_ensemble(db, bad_mid)
        _seed_trade(db, bad_mid, bad_ep, "polymarket")

        good_mid = _seed_market(db, "polymarket", "pm-ok")
        good_ep = _seed_ensemble(db, good_mid)
        _seed_trade(db, good_mid, good_ep, "polymarket")

        resolver = OutcomeResolver(db, settings)
        result = ResolverResult()

        bad_resp = Mock()
        bad_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Client error '422 Unprocessable Entity' for url",
            request=httpx.Request("GET", "https://gamma-api.polymarket.com/markets/pm-422"),
            response=httpx.Response(422),
        )

        bad_clob_resp = Mock()
        bad_clob_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Client error '404 Not Found' for url",
            request=httpx.Request("GET", "https://clob.polymarket.com/markets/pm-422"),
            response=httpx.Response(404),
        )

        good_resp = Mock()
        good_resp.raise_for_status.return_value = None
        good_resp.json.return_value = {"resolved": True, "outcome": "YES"}

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[bad_resp, bad_clob_resp, good_resp])

        await resolver._fetch_polymarket_resolutions(
            client,
            [(bad_mid, "pm-422"), (good_mid, "pm-ok")],
            result,
        )

        assert any("pm-422" in err for err in result.errors)
        assert result.markets_resolved == 1
        assert result.trades_settled == 1

        outcome = db.execute(
            "SELECT outcome FROM outcomes WHERE market_id = ?",
            (good_mid,),
        ).fetchone()
        assert outcome is not None
        assert outcome[0] == "yes"

    async def test_falls_back_to_clob_and_settles_on_gamma_422(self, db, settings):
        mid = _seed_market(db, "polymarket", "pm-clob-win")
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "polymarket", "buy_no", 10.0, 0.40, 0.006)

        resolver = OutcomeResolver(db, settings)
        result = ResolverResult()

        gamma_422 = Mock()
        gamma_422.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Client error '422 Unprocessable Entity' for url",
            request=httpx.Request("GET", "https://gamma-api.polymarket.com/markets/pm-clob-win"),
            response=httpx.Response(422),
        )

        clob_ok = Mock()
        clob_ok.raise_for_status.return_value = None
        clob_ok.json.return_value = {
            "closed": True,
            "tokens": [
                {"outcome": "Yes", "winner": False},
                {"outcome": "No", "winner": True},
            ],
        }

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[gamma_422, clob_ok])

        await resolver._fetch_polymarket_resolutions(client, [(mid, "pm-clob-win")], result)

        assert result.markets_resolved == 1
        assert result.trades_settled == 1
        outcome = db.execute("SELECT outcome FROM outcomes WHERE market_id = ?", (mid,)).fetchone()
        assert outcome is not None
        assert outcome[0] == "no"

        trade = db.execute("SELECT status FROM trades WHERE market_id = ?", (mid,)).fetchone()
        assert trade is not None
        assert trade[0] == "settled"

    async def test_does_not_settle_when_clob_market_still_open(self, db, settings):
        mid = _seed_market(db, "polymarket", "pm-clob-open")
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "polymarket")

        resolver = OutcomeResolver(db, settings)
        result = ResolverResult()

        gamma_422 = Mock()
        gamma_422.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Client error '422 Unprocessable Entity' for url",
            request=httpx.Request("GET", "https://gamma-api.polymarket.com/markets/pm-clob-open"),
            response=httpx.Response(422),
        )

        clob_open = Mock()
        clob_open.raise_for_status.return_value = None
        clob_open.json.return_value = {
            "closed": False,
            "tokens": [{"outcome": "Yes", "winner": False}, {"outcome": "No", "winner": False}],
        }

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[gamma_422, clob_open])

        await resolver._fetch_polymarket_resolutions(client, [(mid, "pm-clob-open")], result)

        assert result.markets_resolved == 0
        assert result.trades_settled == 0
        outcome = db.execute("SELECT outcome FROM outcomes WHERE market_id = ?", (mid,)).fetchone()
        assert outcome is None

        trade = db.execute("SELECT status FROM trades WHERE market_id = ?", (mid,)).fetchone()
        assert trade is not None
        assert trade[0] == "open"


class TestKalshiCloseDateFallback:
    """Tests for the close_date fallback in _fetch_kalshi_resolutions."""

    def _seed_kalshi_market(
        self,
        conn: sqlite3.Connection,
        ticker: str,
        current_price: float,
        close_date: datetime,
    ) -> int:
        cur = conn.execute(
            """INSERT INTO markets
               (platform, platform_id, question, current_price, volume,
                close_date, fetched_at)
               VALUES ('kalshi', ?, 'Test Kalshi market', ?, 10000, ?, ?)""",
            (ticker, current_price, close_date.isoformat(), close_date.isoformat()),
        )
        conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    @pytest.mark.asyncio
    async def test_price_at_floor_resolves_no(self, db, settings):
        """price <= 0.05 with past close_date -> inferred outcome 'no'."""
        past = datetime(2026, 5, 1, tzinfo=timezone.utc)
        mid = self._seed_kalshi_market(db, "PGA-ABERG-26", 0.03, past)
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "kalshi", "buy_no", 10.0, 0.97, 0.0)

        resolver = OutcomeResolver(db, settings)
        result = ResolverResult()
        client = AsyncMock()
        client.get.side_effect = Exception("[Errno -2] Name or service not known")

        await resolver._fetch_kalshi_resolutions(client, [(mid, "PGA-ABERG-26")], result)

        assert result.markets_resolved == 1
        assert result.trades_settled == 1
        outcome = db.execute(
            "SELECT outcome FROM outcomes WHERE market_id = ?", (mid,)
        ).fetchone()
        assert outcome[0] == "no"
        trade = db.execute(
            "SELECT status FROM trades WHERE market_id = ?", (mid,)
        ).fetchone()
        assert trade[0] == "settled"

    @pytest.mark.asyncio
    async def test_price_at_ceiling_resolves_yes(self, db, settings):
        """price >= 0.95 with past close_date -> inferred outcome 'yes'."""
        past = datetime(2026, 5, 1, tzinfo=timezone.utc)
        mid = self._seed_kalshi_market(db, "SOME-YES-26", 0.97, past)
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "kalshi", "buy_yes", 10.0, 0.03, 0.0)

        resolver = OutcomeResolver(db, settings)
        result = ResolverResult()
        client = AsyncMock()
        client.get.side_effect = Exception("[Errno -2] Name or service not known")

        await resolver._fetch_kalshi_resolutions(client, [(mid, "SOME-YES-26")], result)

        assert result.markets_resolved == 1
        assert result.trades_settled == 1
        outcome = db.execute(
            "SELECT outcome FROM outcomes WHERE market_id = ?", (mid,)
        ).fetchone()
        assert outcome[0] == "yes"

    @pytest.mark.asyncio
    async def test_mid_range_price_stays_open(self, db, settings):
        """price in 0.06..0.94 range -> ambiguous, trade left open."""
        past = datetime(2026, 5, 1, tzinfo=timezone.utc)
        mid = self._seed_kalshi_market(db, "AMBIG-26", 0.50, past)
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "kalshi", "buy_yes", 10.0, 0.50, 0.0)

        resolver = OutcomeResolver(db, settings)
        result = ResolverResult()
        client = AsyncMock()
        client.get.side_effect = Exception("[Errno -2] Name or service not known")

        await resolver._fetch_kalshi_resolutions(client, [(mid, "AMBIG-26")], result)

        assert result.markets_resolved == 0
        assert result.trades_settled == 0
        trade = db.execute(
            "SELECT status FROM trades WHERE market_id = ?", (mid,)
        ).fetchone()
        assert trade[0] == "open"

    @pytest.mark.asyncio
    async def test_future_close_date_no_fallback(self, db, settings):
        """price <= 0.05 but close_date is in the future -> no fallback fires."""
        future = datetime(2027, 1, 1, tzinfo=timezone.utc)
        mid = self._seed_kalshi_market(db, "FUTURE-26", 0.02, future)
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "kalshi", "buy_no", 10.0, 0.98, 0.0)

        resolver = OutcomeResolver(db, settings)
        result = ResolverResult()
        client = AsyncMock()
        client.get.side_effect = Exception("[Errno -2] Name or service not known")

        await resolver._fetch_kalshi_resolutions(client, [(mid, "FUTURE-26")], result)

        assert result.markets_resolved == 0
        assert result.trades_settled == 0
        trade = db.execute(
            "SELECT status FROM trades WHERE market_id = ?", (mid,)
        ).fetchone()
        assert trade[0] == "open"

    @pytest.mark.asyncio
    async def test_api_settled_skips_fallback(self, db, settings):
        """When API returns status=settled, the API outcome wins; no fallback."""
        past = datetime(2026, 5, 1, tzinfo=timezone.utc)
        # Seed with mid-range price so fallback would leave open if it fired
        mid = self._seed_kalshi_market(db, "SETTLED-API-26", 0.50, past)
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "kalshi", "buy_yes", 10.0, 0.50, 0.0)

        resolver = OutcomeResolver(db, settings)
        result = ResolverResult()
        mock_resp = Mock()
        mock_resp.raise_for_status.return_value = None
        mock_resp.json.return_value = {"market": {"status": "settled", "result": "yes"}}
        client = AsyncMock()
        client.get.return_value = mock_resp

        await resolver._fetch_kalshi_resolutions(client, [(mid, "SETTLED-API-26")], result)

        assert result.markets_resolved == 1
        assert result.trades_settled == 1
        outcome = db.execute(
            "SELECT outcome FROM outcomes WHERE market_id = ?", (mid,)
        ).fetchone()
        assert outcome[0] == "yes"


class TestSettleStuckKalshiOverrides:
    """Tests for the one-off migration function."""

    def test_settles_open_trade_with_known_outcome(self, db, settings):
        mid = _seed_market(db, "kalshi", "STUCK-TEST")
        ep_id = _seed_ensemble(db, mid)
        trade_id = _seed_trade(db, mid, ep_id, "kalshi", "buy_no", 10.0, 0.97, 0.0)

        resolver = OutcomeResolver(db, settings)
        resolver._KALSHI_STUCK_OVERRIDES = {trade_id: "no"}  # type: ignore[assignment]

        result = ResolverResult()
        resolver._settle_stuck_kalshi_overrides(result)

        assert result.trades_settled == 1
        assert result.markets_resolved == 1
        row = db.execute(
            "SELECT status, pnl FROM trades WHERE id = ?", (trade_id,)
        ).fetchone()
        assert row[0] == "settled"
        # buy_no + no outcome -> profit
        assert row[1] > 0

    def test_idempotent_on_already_settled_trade(self, db, settings):
        mid = _seed_market(db, "kalshi", "STUCK-IDEM")
        ep_id = _seed_ensemble(db, mid)
        trade_id = _seed_trade(db, mid, ep_id, "kalshi", "buy_no", 10.0, 0.97, 0.0)

        resolver = OutcomeResolver(db, settings)
        resolver._KALSHI_STUCK_OVERRIDES = {trade_id: "no"}  # type: ignore[assignment]

        r1 = ResolverResult()
        resolver._settle_stuck_kalshi_overrides(r1)
        assert r1.trades_settled == 1

        r2 = ResolverResult()
        resolver._settle_stuck_kalshi_overrides(r2)
        assert r2.trades_settled == 0
        assert r2.markets_resolved == 0

    def test_duplicate_market_positions_settled_together(self, db, settings):
        """Trades sharing the same market_id are all settled on first hit."""
        mid = _seed_market(db, "kalshi", "HORMUZ-DUP")
        ep_id = _seed_ensemble(db, mid)
        t1 = _seed_trade(db, mid, ep_id, "kalshi", "buy_yes", 10.0, 0.50, 0.0)
        t2 = _seed_trade(db, mid, ep_id, "kalshi", "buy_yes", 10.0, 0.50, 0.0)
        t3 = _seed_trade(db, mid, ep_id, "kalshi", "buy_yes", 10.0, 0.50, 0.0)

        resolver = OutcomeResolver(db, settings)
        # Simulate trades 24/25/27 — same market, three separate trade IDs
        resolver._KALSHI_STUCK_OVERRIDES = {  # type: ignore[assignment]
            t1: "no",
            t2: "no",
            t3: "no",
        }

        result = ResolverResult()
        resolver._settle_stuck_kalshi_overrides(result)

        # All 3 trades settled, but market only counted once
        assert result.trades_settled == 3
        assert result.markets_resolved == 1
        rows = db.execute(
            "SELECT status FROM trades WHERE market_id = ?", (mid,)
        ).fetchall()
        assert all(r[0] == "settled" for r in rows)


@pytest.mark.asyncio
class TestPolymarketClosedWithoutWinner:
    """Regression: clob closed but no token winner — trade must stay open."""

    async def test_does_not_settle_when_clob_closed_without_winner(self, db, settings):
        mid = _seed_market(db, "polymarket", "pm-clob-nowinner")
        ep_id = _seed_ensemble(db, mid)
        _seed_trade(db, mid, ep_id, "polymarket")

        resolver = OutcomeResolver(db, settings)
        result = ResolverResult()

        gamma_422 = Mock()
        gamma_422.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Client error '422 Unprocessable Entity' for url",
            request=httpx.Request("GET", "https://gamma-api.polymarket.com/markets/pm-clob-nowinner"),
            response=httpx.Response(422),
        )

        clob_closed_no_winner = Mock()
        clob_closed_no_winner.raise_for_status.return_value = None
        clob_closed_no_winner.json.return_value = {
            "closed": True,
            "tokens": [{"outcome": "Yes", "winner": False}, {"outcome": "No", "winner": False}],
        }

        client = AsyncMock()
        client.get = AsyncMock(side_effect=[gamma_422, clob_closed_no_winner])

        await resolver._fetch_polymarket_resolutions(client, [(mid, "pm-clob-nowinner")], result)

        assert result.markets_resolved == 0
        assert result.trades_settled == 0
        outcome = db.execute("SELECT outcome FROM outcomes WHERE market_id = ?", (mid,)).fetchone()
        assert outcome is None

        trade = db.execute("SELECT status FROM trades WHERE market_id = ?", (mid,)).fetchone()
        assert trade is not None
        assert trade[0] == "open"
