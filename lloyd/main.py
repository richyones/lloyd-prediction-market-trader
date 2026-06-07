from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import structlog

from lloyd.common.models import ScanResult
from lloyd.config import get_settings
from lloyd.db import (
    flag_large_move,
    get_connection,
    get_latest_markets,
    get_market_info,
    get_open_paper_trades,
    get_recent_scan_results,
    init_db,
    insert_market_pairs,
    insert_markets,
    insert_scan_results,
)
from lloyd.scanner.kalshi import KalshiClient
from lloyd.scanner.matcher import MarketMatcher
from lloyd.scanner.polymarket import PolymarketClient
from lloyd.scanner.scanner import MarketScanner

DASHBOARD_PATH = Path(__file__).resolve().parent.parent / "lloyd" / "dashboard.html"

log = structlog.get_logger()

_shutdown_requested = False


def _configure_logging(level: str) -> None:
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer()
            if level == "DEBUG"
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            structlog.stdlib.NAME_TO_LEVEL.get(level, 20)
        ),
    )


async def run_scan_cycle() -> None:
    settings = get_settings()

    log.info("scan_cycle_start")

    poly_client = PolymarketClient()
    kalshi_client = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        rsa_key_path=settings.kalshi_rsa_key_path,
        rsa_key_content=settings.kalshi_rsa_key_content,
    )

    try:
        poly_result, kalshi_result = await asyncio.gather(
            poly_client.fetch_all_markets(),
            kalshi_client.fetch_all_markets(),
            return_exceptions=True,
        )

        if isinstance(poly_result, BaseException):
            raise poly_result
        poly_markets = poly_result

        if isinstance(kalshi_result, BaseException):
            log.warning(
                "kalshi_fetch_failed",
                error=str(kalshi_result),
            )
            kalshi_markets = []
        else:
            kalshi_markets = kalshi_result

        all_markets = poly_markets + kalshi_markets
        log.info(
            "markets_fetched",
            polymarket=len(poly_markets),
            kalshi=len(kalshi_markets),
            total=len(all_markets),
        )

        scanner = MarketScanner(settings)
        results = scanner.scan(all_markets)

        conn = get_connection(settings.database_path)
        try:
            init_db(conn)
            insert_markets(conn, [r.market for r in results])
            insert_scan_results(conn, results)
        finally:
            conn.close()

        print_summary(results)
    finally:
        await poly_client.close()
        await kalshi_client.close()

    log.info("scan_cycle_complete")


async def run_prediction_cycle() -> None:
    """Run LLM prediction + trading independently of the scan cycle."""
    settings = get_settings()
    log.info("prediction_cycle_start")

    conn = get_connection(settings.database_path)
    poly_client = PolymarketClient()
    kalshi_client = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        rsa_key_path=settings.kalshi_rsa_key_path,
        rsa_key_content=settings.kalshi_rsa_key_content,
    )

    try:
        init_db(conn)
        results = get_recent_scan_results(conn, settings.max_prediction_candidates)

        if not results:
            log.info("prediction_cycle_no_candidates")
            return

        from lloyd.postmortem.calibration import CalibrationAnalyzer
        from lloyd.prediction.ensemble import EnsemblePipeline

        model_weights = CalibrationAnalyzer(conn, settings).get_model_weights()
        pipeline = EnsemblePipeline(conn, settings)
        log.info(
            "pipeline_candidates",
            total_scanned=len(results),
            feeding_to_llm=len(results),
        )
        predictions = await pipeline.run(results, model_weights=model_weights or None)

        await _run_stage3(conn, settings, predictions, poly_client, kalshi_client)
    finally:
        await poly_client.close()
        await kalshi_client.close()
        conn.close()

    log.info("prediction_cycle_complete")


async def _run_stage3(
    conn,
    settings,
    predictions: list,
    poly_client: PolymarketClient,
    kalshi_client: KalshiClient,
) -> None:
    """Risk sizing and paper execution for all actionable predictions."""
    from lloyd.execution.kalshi_live import KalshiLiveExecutor
    from lloyd.execution.paper import PaperExecutor
    from lloyd.execution.polymarket_live import PolymarketLiveExecutor
    from lloyd.risk.sizer import RiskSizer

    sizer = RiskSizer(settings)

    if settings.live_trading_enabled:
        log.warning("live_trading_enabled_stub")
    executor = PaperExecutor(
        conn, settings, polymarket_client=poly_client, kalshi_client=kalshi_client,
    )

    trades_placed = 0
    trades_blocked = 0

    for prediction in predictions:
        if prediction.trade_signal == "no_trade":
            continue

        market_info = get_market_info(conn, prediction.market_id)
        if market_info is None:
            log.warning("market_info_not_found", market_id=prediction.market_id)
            continue

        platform, platform_id, category = market_info

        if settings.live_trading_enabled:
            if platform == "polymarket":
                live_executor = PolymarketLiveExecutor()
            else:
                live_executor = KalshiLiveExecutor()
            current_executor = live_executor
        else:
            current_executor = executor

        portfolio_state = executor.get_portfolio_state()
        trade_signal = sizer.size(
            prediction,
            portfolio_state,
            platform=platform,
            platform_id=platform_id,
            category=category,
        )

        if trade_signal is None:
            trades_blocked += 1
            continue

        result = await current_executor.execute(trade_signal)
        trades_placed += 1
        log.info(
            "trade_placed",
            platform=result.platform,
            direction=result.direction,
            quantity=round(result.quantity, 4),
            executed_price=round(result.executed_price, 4),
            fee=round(result.fee, 6),
            slippage=round(result.slippage, 6),
            is_paper=result.is_paper,
        )

    executor.snapshot_portfolio()

    state = executor.get_portfolio_state()
    log.info(
        "stage_3_complete",
        trades_placed=trades_placed,
        trades_blocked=trades_blocked,
        cash_balance=round(state.cash_balance, 2),
        total_exposure=round(state.total_exposure, 2),
    )


async def _price_check_job() -> None:
    """Check open positions for large price moves and snapshot the portfolio."""
    settings = get_settings()
    conn = get_connection(settings.database_path)

    poly_client = PolymarketClient()
    kalshi_client = KalshiClient(
        base_url=settings.kalshi_base_url,
        api_key_id=settings.kalshi_api_key_id,
        rsa_key_path=settings.kalshi_rsa_key_path,
        rsa_key_content=settings.kalshi_rsa_key_content,
    )

    try:
        from lloyd.execution.paper import PaperExecutor

        executor = PaperExecutor(
            conn, settings, polymarket_client=poly_client, kalshi_client=kalshi_client,
        )

        open_trades = get_open_paper_trades(conn)
        if not open_trades:
            return

        unrealized_prices: dict[int, float] = {}

        for trade in open_trades:
            current_price = await executor.get_current_price(
                trade.market_id, trade.platform, trade.platform_id,
            )
            if current_price is None:
                continue

            unrealized_prices[trade.id] = current_price
            move = abs(current_price - trade.executed_price)
            if move >= settings.large_move_threshold:
                flag_large_move(conn, trade.id)
                log.warning(
                    "large_move_flagged",
                    trade_id=trade.id,
                    move=round(move, 4),
                    entry=trade.executed_price,
                    current=current_price,
                )

        executor.snapshot_portfolio(unrealized_prices=unrealized_prices or None)
    finally:
        await poly_client.close()
        await kalshi_client.close()
        conn.close()


def _build_api_data() -> str:
    """Query SQLite and return all dashboard data as a JSON string."""
    settings = get_settings()
    conn = get_connection(settings.database_path)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)

        row = conn.execute(
            "SELECT cash_balance, total_exposure, unrealized_pnl, "
            "realized_pnl, num_open_positions "
            "FROM portfolio ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        portfolio = dict(row) if row else None

        open_trades = [
            dict(r) for r in conn.execute(
                "SELECT t.id, m.question, m.platform, t.direction, t.quantity, "
                "t.executed_price, t.fee, t.opened_at, t.large_move_flagged, "
                "m.close_date, m.category "
                "FROM trades t "
                "JOIN markets m ON m.id = t.market_id "
                "WHERE t.status = 'open' AND t.is_paper = 1 "
                "ORDER BY t.opened_at DESC"
            ).fetchall()
        ]

        recent_predictions = [
            dict(r) for r in conn.execute(
                "SELECT ep.trade_signal, ep.edge, ep.final_probability, "
                "ep.market_price, ep.tier2_used, ep.created_at, "
                "m.question, m.platform, "
                "SUM(p.cost_usd) as total_cost "
                "FROM ensemble_predictions ep "
                "JOIN markets m ON m.id = ep.market_id "
                "LEFT JOIN predictions p ON p.market_id = ep.market_id "
                "GROUP BY ep.id "
                "ORDER BY ep.created_at DESC "
                "LIMIT 40"
            ).fetchall()
        ]

        settled_trades = [
            dict(r) for r in conn.execute(
                "SELECT t.direction, t.executed_price, t.pnl, t.closed_at, "
                "m.question, m.platform, o.outcome "
                "FROM trades t "
                "JOIN markets m ON m.id = t.market_id "
                "LEFT JOIN outcomes o ON o.market_id = t.market_id "
                "WHERE t.status = 'settled' AND t.is_paper = 1 "
                "ORDER BY t.closed_at DESC"
            ).fetchall()
        ]

        cost_by_day = [
            dict(r) for r in conn.execute(
                "SELECT DATE(created_at) AS day, SUM(cost_usd) AS daily_cost, "
                "COUNT(*) AS predictions "
                "FROM predictions "
                "WHERE created_at >= DATE('now', '-7 days') "
                "GROUP BY DATE(created_at) "
                "ORDER BY day DESC"
            ).fetchall()
        ]

        model_scores = [
            dict(r) for r in conn.execute(
                "SELECT model_name, category, period_type, brier_score, "
                "calibration_error, num_predictions, period_end "
                "FROM model_scores ORDER BY period_end DESC"
            ).fetchall()
        ]

    finally:
        conn.close()

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "portfolio": portfolio,
        "open_trades": open_trades,
        "recent_predictions": recent_predictions,
        "cost_by_day": cost_by_day,
        "settled_trades": settled_trades,
        "model_scores": model_scores,
    }
    return json.dumps(payload)


def _http_response(status: str, content_type: str, body: bytes, extra_headers: str = "") -> bytes:
    """Build a minimal HTTP/1.1 response."""
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(body)}\r\n"
        f"{extra_headers}"
        "Connection: close\r\n"
        "\r\n"
    ).encode() + body


async def _health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Minimal HTTP router for dashboard, health check, and API data."""
    try:
        raw = await reader.read(8192)
        request_line = raw.split(b"\r\n", 1)[0].decode("utf-8", errors="replace")
        parts = request_line.split()
        method = parts[0] if len(parts) >= 1 else ""
        path = parts[1] if len(parts) >= 2 else ""

        if path == "/health":
            body = b'{"status":"ok"}'
            writer.write(_http_response("200 OK", "application/json", body))

        elif path == "/":
            if DASHBOARD_PATH.is_file():
                html = DASHBOARD_PATH.read_bytes()
                writer.write(_http_response("200 OK", "text/html; charset=utf-8", html))
            else:
                body = b'Not found'
                writer.write(_http_response("404 Not Found", "text/plain", body))

        elif path == "/api/data":
            try:
                loop = asyncio.get_event_loop()
                body = await loop.run_in_executor(None, lambda: _build_api_data().encode("utf-8"))
                writer.write(_http_response(
                    "200 OK", "application/json", body,
                    "Access-Control-Allow-Origin: *\r\n"
                    "Access-Control-Allow-Methods: GET\r\n",
                ))
            except Exception as exc:
                log.error("api_data_error", error=str(exc))
                body = json.dumps({"error": str(exc)}).encode("utf-8")
                writer.write(_http_response(
                    "500 Internal Server Error", "application/json", body,
                    "Access-Control-Allow-Origin: *\r\n"
                    "Access-Control-Allow-Methods: GET\r\n",
                ))

        else:
            body = b'Not found'
            writer.write(_http_response("404 Not Found", "text/plain", body))

        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def print_summary(results: list[ScanResult]) -> None:
    top = results[:20]
    if not top:
        print("\nNo markets passed filters.\n")
        return

    header = f"{'Rank':>4} | {'Platform':<12} | {'Question':<55} | {'Price':>5} | {'Volume':>12} | {'Score':>5}"
    sep = f"{'----':>4} | {'------------':<12} | {'-' * 55} | {'-----':>5} | {'------------':>12} | {'-----':>5}"
    print(f"\n{header}")
    print(sep)
    for i, r in enumerate(top, 1):
        q = r.market.question[:52] + "..." if len(r.market.question) > 55 else r.market.question
        print(
            f"{i:>4} | {r.market.platform:<12} | {q:<55} | "
            f"{r.market.current_price:>5.2f} | {r.market.volume:>12,.0f} | "
            f"{r.exploitability_score:>5.2f}"
        )
    print()


def cli() -> None:
    parser = argparse.ArgumentParser(prog="lloyd", description="Lloyd prediction market scanner")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("scan", help="Run a single scan cycle")
    sub.add_parser("run", help="Start the scheduler loop")

    args = parser.parse_args()

    _configure_logging(get_settings().log_level)

    if args.command == "scan":
        asyncio.run(run_scan_cycle())
    elif args.command == "run":
        _start_scheduler()
    else:
        parser.print_help()
        sys.exit(1)


async def _matcher_job() -> None:
    settings = get_settings()
    conn = get_connection(settings.database_path)
    try:
        init_db(conn)
        markets = get_latest_markets(conn)
        poly_markets = [m for m in markets if m.platform == "polymarket"]
        kalshi_markets = [m for m in markets if m.platform == "kalshi"]

        if not poly_markets and not kalshi_markets:
            log.info(
                "matcher_job_skipped",
                reason="no_polymarket_markets_and_no_kalshi_markets",
            )
            return
        if not poly_markets:
            log.info("matcher_job_skipped", reason="no_polymarket_markets")
            return
        if not kalshi_markets:
            log.info("matcher_job_skipped", reason="no_kalshi_markets")
            return

        matcher = MarketMatcher()
        loop = asyncio.get_running_loop()
        pairs = await loop.run_in_executor(
            None, lambda: matcher.match(poly_markets, kalshi_markets)
        )
        if pairs:
            insert_market_pairs(conn, pairs)
        log.info(
            "matcher_job_complete",
            pairs_found=len(pairs),
            poly_markets_count=len(poly_markets),
            kalshi_markets_count=len(kalshi_markets),
        )
    except Exception as exc:
        log.error("matcher_job_failed", error=str(exc))
    finally:
        conn.close()


async def _resolver_job() -> None:
    """Async resolver job — requires AsyncIOScheduler (used in Stage 3)."""
    log.info("resolver_job_started")
    settings = get_settings()
    conn = get_connection(settings.database_path)
    try:
        init_db(conn)
        from lloyd.postmortem.resolver import OutcomeResolver

        resolver = OutcomeResolver(conn, settings)
        result = await resolver.run()
        log.info(
            "resolver_job_complete",
            markets_resolved=result.markets_resolved,
            trades_settled=result.trades_settled,
            total_pnl=round(result.total_pnl_realized, 4),
        )
    except Exception as exc:
        log.error("resolver_job_failed", error=str(exc))
    finally:
        conn.close()


def _calibration_job() -> None:
    """Sync calibration job — runs daily at 02:00 UTC."""
    settings = get_settings()
    conn = get_connection(settings.database_path)
    try:
        init_db(conn)
        from lloyd.postmortem.calibration import CalibrationAnalyzer

        CalibrationAnalyzer(conn, settings).run()
        log.info("calibration_job_complete")
    except Exception as exc:
        log.error("calibration_job_failed", error=str(exc))
    finally:
        conn.close()


def _start_scheduler() -> None:
    from apscheduler.schedulers.asyncio import AsyncIOScheduler

    settings = get_settings()

    async def _main() -> None:
        global _shutdown_requested

        loop = asyncio.get_running_loop()
        scheduler = AsyncIOScheduler()
        health_server = None

        # --- Health check HTTP server ---
        try:
            health_server = await asyncio.start_server(
                _health_handler, "0.0.0.0", settings.health_check_port,
            )
            log.info("health_server_started", port=settings.health_check_port)
        except OSError as exc:
            log.warning("health_server_failed", error=str(exc))

        # --- SIGTERM / SIGINT handler ---
        def _handle_shutdown(signum: int, frame: object) -> None:
            global _shutdown_requested
            _shutdown_requested = True
            log.info("shutdown_requested", signal=signum)

        signal.signal(signal.SIGTERM, _handle_shutdown)
        signal.signal(signal.SIGINT, _handle_shutdown)

        # --- Scheduler jobs ---
        scheduler.add_job(
            run_scan_cycle,
            "interval",
            minutes=settings.scan_interval_minutes,
        )
        scheduler.add_job(
            run_prediction_cycle,
            "interval",
            hours=settings.prediction_interval_hours,
        )
        scheduler.add_job(
            _price_check_job,
            "interval",
            minutes=settings.price_check_interval_minutes,
        )
        # Stage 4: resolver (async) runs every 15 min via AsyncIOScheduler
        scheduler.add_job(
            _resolver_job,
            "interval",
            minutes=15,
        )
        # Stage 4: calibration (sync) runs daily at 02:00 UTC
        scheduler.add_job(
            _calibration_job,
            "cron",
            hour=2,
            minute=0,
        )
        scheduler.add_job(
            _matcher_job,
            "interval",
            hours=settings.matcher_interval_hours,
        )
        scheduler.start()
        log.info(
            "scheduler_started",
            scan_interval_minutes=settings.scan_interval_minutes,
            prediction_interval_hours=settings.prediction_interval_hours,
            price_check_interval_minutes=settings.price_check_interval_minutes,
            matcher_interval_hours=settings.matcher_interval_hours,
        )

        # Run one scan + prediction cycle immediately on startup
        await run_scan_cycle()
        await run_prediction_cycle()
        asyncio.create_task(_matcher_job())
        asyncio.create_task(_resolver_job())

        # Keep alive until shutdown
        while not _shutdown_requested:
            await asyncio.sleep(1)

        # Graceful shutdown
        log.info("shutting_down")
        scheduler.shutdown(wait=True)
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()

    try:
        asyncio.run(_main())
    except (KeyboardInterrupt, SystemExit):
        log.info("scheduler_stopped")
