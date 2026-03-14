from __future__ import annotations

import argparse
import asyncio
import signal
import sys

import structlog

from lloyd.common.models import MarketPair, ScanResult
from lloyd.config import get_settings
from lloyd.db import (
    flag_large_move,
    get_connection,
    get_market_info,
    get_open_paper_trades,
    init_db,
    insert_market_pairs,
    insert_markets,
    insert_scan_results,
)
from lloyd.scanner.kalshi import KalshiClient
from lloyd.scanner.matcher import MarketMatcher
from lloyd.scanner.polymarket import PolymarketClient
from lloyd.scanner.scanner import MarketScanner

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
            structlog.stdlib._NAME_TO_LEVEL.get(level, 20)
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
        poly_markets, kalshi_markets = await asyncio.gather(
            poly_client.fetch_all_markets(),
            kalshi_client.fetch_all_markets(),
        )

        all_markets = poly_markets + kalshi_markets
        log.info(
            "markets_fetched",
            polymarket=len(poly_markets),
            kalshi=len(kalshi_markets),
            total=len(all_markets),
        )

        scanner = MarketScanner(settings)
        results = scanner.scan(all_markets)

        matcher = MarketMatcher()
        pairs = matcher.match(poly_markets, kalshi_markets)

        conn = get_connection(settings.database_path)
        try:
            init_db(conn)
            insert_markets(conn, [r.market for r in results])
            insert_scan_results(conn, results)
            if pairs:
                insert_market_pairs(conn, pairs)

            # --- Stage 2: research + prediction pipeline ---
            if results:
                from lloyd.postmortem.calibration import CalibrationAnalyzer
                from lloyd.prediction.ensemble import EnsemblePipeline

                model_weights = CalibrationAnalyzer(conn, settings).get_model_weights()
                pipeline = EnsemblePipeline(conn, settings)
                predictions = await pipeline.run(results, model_weights=model_weights or None)

                # --- Stage 3: risk sizing + paper execution ---
                await _run_stage3(conn, settings, predictions, poly_client, kalshi_client)
        finally:
            conn.close()

        print_summary(results, pairs)
    finally:
        await poly_client.close()
        await kalshi_client.close()

    log.info("scan_cycle_complete")


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


async def _health_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """Minimal HTTP handler for Railway health checks."""
    try:
        await reader.read(4096)
        response = (
            "HTTP/1.1 200 OK\r\n"
            "Content-Type: application/json\r\n"
            "Content-Length: 15\r\n"
            "\r\n"
            '{"status":"ok"}'
        )
        writer.write(response.encode())
        await writer.drain()
    finally:
        writer.close()
        await writer.wait_closed()


def print_summary(results: list[ScanResult], pairs: list[MarketPair]) -> None:
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

    if pairs:
        print(f"Cross-platform matches: {len(pairs)}")
        for p in pairs[:10]:
            print(
                f"  [{p.similarity_score:.0f}%] Δ{p.price_divergence:.2f}  "
                f"PM: {p.polymarket_market.question[:40]}  ←→  "
                f"KA: {p.kalshi_market.question[:40]}"
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


async def _resolver_job() -> None:
    """Async resolver job — requires AsyncIOScheduler (used in Stage 3)."""
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
        scheduler.start()
        log.info(
            "scheduler_started",
            scan_interval_minutes=settings.scan_interval_minutes,
            price_check_interval_minutes=settings.price_check_interval_minutes,
        )

        # Run one scan immediately
        await run_scan_cycle()

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
