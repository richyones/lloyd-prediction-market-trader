"""Rich terminal dashboard and markdown export."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import structlog

from lloyd.config import Settings
from lloyd.postmortem.calibration import CalibrationAnalyzer, OVERALL_CATEGORY_SENTINEL
from lloyd.postmortem.metrics import MetricsCalculator

log = structlog.get_logger()

PRICE_STALENESS_HOURS = 2


class Dashboard:
    def __init__(self, conn: sqlite3.Connection, settings: Settings) -> None:
        self._conn = conn
        self._settings = settings

    def render(self) -> None:
        from rich.console import Console
        from rich.layout import Layout
        from rich.panel import Panel

        console = Console()
        layout = Layout()

        layout.split_column(
            Layout(name="top", size=3),
            Layout(name="body"),
        )
        layout["body"].split_row(
            Layout(name="left"),
            Layout(name="right"),
        )

        layout["top"].update(Panel("[bold]Lloyd Performance Dashboard[/bold]", style="blue"))
        layout["left"].split_column(
            Layout(self._portfolio_panel(), name="portfolio"),
            Layout(self._open_positions_panel(), name="positions"),
            Layout(self._todays_trades_panel(), name="today"),
        )
        layout["right"].split_column(
            Layout(self._overall_pnl_panel(), name="pnl"),
            Layout(self._brier_panel(), name="brier"),
            Layout(self._calibration_plot(), name="cal"),
            Layout(self._top_trades_panel(), name="top_trades"),
        )

        console.print(layout)

    def export_markdown(self, path: str) -> None:
        sections = [
            "# Lloyd Performance Dashboard",
            f"_Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}_",
            "",
            self._md_portfolio(),
            self._md_open_positions(),
            self._md_overall_pnl(),
            self._md_brier(),
            self._md_top_trades(),
        ]
        with open(path, "w") as f:
            f.write("\n\n".join(sections))

    def _portfolio_panel(self):
        from rich.panel import Panel
        from rich.table import Table

        row = self._conn.execute(
            "SELECT cash_balance, total_exposure, unrealized_pnl, num_open_positions "
            "FROM portfolio ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()

        if not row:
            return Panel("No portfolio data", title="Portfolio")

        table = Table(show_header=False)
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Cash", f"${row[0]:,.2f}")
        table.add_row("Exposure", f"${row[1]:,.2f}")
        table.add_row("Unrealized P&L", f"${row[2]:,.2f}" if row[2] is not None else "—")
        table.add_row("Open Positions", str(row[3] or 0))

        return Panel(table, title="Portfolio")

    def _open_positions_panel(self):
        from rich.panel import Panel
        from rich.table import Table

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=PRICE_STALENESS_HOURS)).isoformat()

        rows = self._conn.execute(
            """SELECT t.id, m.question, t.direction, t.quantity, t.executed_price,
                      m.current_price, m.fetched_at
               FROM trades t
               JOIN markets m ON m.id = t.market_id
               WHERE t.status = 'open'
               ORDER BY t.opened_at DESC""",
        ).fetchall()

        table = Table(title="Open Positions")
        table.add_column("ID", justify="right")
        table.add_column("Market", max_width=40)
        table.add_column("Dir")
        table.add_column("Qty", justify="right")
        table.add_column("Entry", justify="right")
        table.add_column("Current", justify="right")
        table.add_column("Unrl P&L", justify="right")

        for r in rows:
            trade_id, question, direction, qty, entry, current_price, fetched_at = r
            q_short = (question[:37] + "...") if len(question) > 40 else question

            if current_price is not None and fetched_at and fetched_at >= cutoff:
                if direction == "buy_yes":
                    unrl = (current_price - entry) * qty
                else:
                    unrl = (entry - current_price) * qty
                curr_str = f"${current_price:.4f}"
                unrl_str = f"${unrl:+,.4f}"
            else:
                curr_str = "—"
                unrl_str = "—"

            table.add_row(
                str(trade_id), q_short, direction,
                f"{qty:.2f}", f"${entry:.4f}", curr_str, unrl_str,
            )

        if not rows:
            return Panel("No open positions", title="Open Positions")

        return Panel(table)

    def _todays_trades_panel(self):
        from rich.panel import Panel
        from rich.table import Table

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows = self._conn.execute(
            """SELECT t.id, m.question, t.direction, t.quantity, t.executed_price, t.status
               FROM trades t
               JOIN markets m ON m.id = t.market_id
               WHERE t.opened_at LIKE ?
               ORDER BY t.opened_at DESC""",
            (f"{today}%",),
        ).fetchall()

        if not rows:
            return Panel("No trades today", title="Today's Trades")

        table = Table(title="Today's Trades")
        table.add_column("ID", justify="right")
        table.add_column("Market", max_width=35)
        table.add_column("Dir")
        table.add_column("Qty", justify="right")
        table.add_column("Price", justify="right")
        table.add_column("Status")

        for r in rows:
            q = (r[1][:32] + "...") if len(r[1]) > 35 else r[1]
            table.add_row(
                str(r[0]), q, r[2], f"{r[3]:.2f}", f"${r[4]:.4f}", r[5],
            )

        return Panel(table)

    def _overall_pnl_panel(self):
        from rich.panel import Panel
        from rich.table import Table

        calc = MetricsCalculator(self._conn, self._settings)
        metrics = calc.compute()

        cutoff = (datetime.now(timezone.utc) - timedelta(hours=PRICE_STALENESS_HOURS)).isoformat()

        unrealized_rows = self._conn.execute(
            """SELECT t.direction, t.quantity, t.executed_price,
                      m.current_price, m.fetched_at
               FROM trades t
               JOIN markets m ON m.id = t.market_id
               WHERE t.status = 'open'""",
        ).fetchall()

        unrealized = 0.0
        has_stale = False
        for direction, qty, entry, current, fetched_at in unrealized_rows:
            if current is None or fetched_at is None or fetched_at < cutoff:
                has_stale = True
                continue
            if direction == "buy_yes":
                unrealized += (current - entry) * qty
            else:
                unrealized += (entry - current) * qty

        table = Table(show_header=False)
        table.add_column("Metric")
        table.add_column("Value", justify="right")
        table.add_row("Realized P&L", f"${metrics.total_pnl:+,.4f}")
        unrl_note = " (excl stale)" if has_stale else ""
        table.add_row("Unrealized P&L", f"${unrealized:+,.4f}{unrl_note}")
        table.add_row("Win Rate", f"{metrics.win_rate:.1%}")
        table.add_row("ROI", f"{metrics.roi:.2%}")
        table.add_row("Pseudo Sharpe", f"{metrics.pseudo_sharpe:.3f}")
        table.add_row("Max Drawdown", f"${metrics.max_drawdown:,.2f}")
        if metrics.mc_max_drawdown_95 is not None:
            table.add_row("MC DD 95th", f"${metrics.mc_max_drawdown_95:,.2f}")
        table.add_row("Kelly Adherence", f"{metrics.kelly_adherence:.1%}")
        table.add_row("Total Trades", str(metrics.total_trades))

        return Panel(table, title="Overall Performance")

    def _brier_panel(self):
        from rich.panel import Panel
        from rich.table import Table

        rows = self._conn.execute(
            """SELECT model_name, period_type, brier_score, calibration_error,
                      num_predictions, period_start, period_end
               FROM model_scores
               WHERE category = ?
               ORDER BY period_end DESC, model_name""",
            (OVERALL_CATEGORY_SENTINEL,),
        ).fetchall()

        if not rows:
            return Panel("No Brier data yet", title="Model Scores")

        table = Table(title="Model Scores (Overall)")
        table.add_column("Model")
        table.add_column("Type")
        table.add_column("Brier", justify="right")
        table.add_column("Cal Err", justify="right")
        table.add_column("N", justify="right")

        for r in rows:
            model, period_type, brier, cal_err, n, _, _ = r
            table.add_row(
                model,
                period_type,
                f"{brier:.4f}",
                f"{cal_err:.4f}" if cal_err is not None else "—",
                str(n),
            )

        return Panel(table)

    def _calibration_plot(self):
        from rich.panel import Panel

        analyzer = CalibrationAnalyzer(self._conn, self._settings)
        models = self._conn.execute(
            "SELECT DISTINCT model_name FROM predictions"
        ).fetchall()

        lines = ["Calibration (predicted vs observed):"]
        for (model_name,) in models:
            preds = analyzer._load_resolved_predictions(model_name)
            if len(preds) < self._settings.min_brier_sample:
                continue
            plot_data = analyzer._calibration_plot_data(preds)
            line_parts = [f"{model_name}:"]
            for mid, obs, count in plot_data:
                bar_len = int(obs * 20)
                bar = "█" * bar_len + "░" * (20 - bar_len)
                line_parts.append(f"  {mid:.1f} |{bar}| {obs:.2f} (n={count})")
            lines.append("\n".join(line_parts))

        return Panel("\n".join(lines) if len(lines) > 1 else "No calibration data", title="Calibration")

    def _top_trades_panel(self):
        from rich.panel import Panel
        from rich.table import Table

        rows = self._conn.execute(
            """SELECT t.id, m.question, t.direction, t.pnl
               FROM trades t
               JOIN markets m ON m.id = t.market_id
               WHERE t.status = 'settled' AND t.pnl IS NOT NULL
               ORDER BY t.pnl DESC
               LIMIT 5""",
        ).fetchall()

        if not rows:
            return Panel("No settled trades", title="Top Trades")

        table = Table(title="Top Trades by P&L")
        table.add_column("ID", justify="right")
        table.add_column("Market", max_width=40)
        table.add_column("Dir")
        table.add_column("P&L", justify="right")

        for r in rows:
            q = (r[1][:37] + "...") if len(r[1]) > 40 else r[1]
            style = "green" if r[3] > 0 else "red"
            table.add_row(str(r[0]), q, r[2], f"${r[3]:+,.4f}", style=style)

        return Panel(table)

    # --- Markdown helpers ---

    def _md_portfolio(self) -> str:
        row = self._conn.execute(
            "SELECT cash_balance, total_exposure, unrealized_pnl, num_open_positions "
            "FROM portfolio ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
        if not row:
            return "## Portfolio\nNo data."
        return (
            f"## Portfolio\n"
            f"- Cash: ${row[0]:,.2f}\n"
            f"- Exposure: ${row[1]:,.2f}\n"
            f"- Unrealized P&L: {'${:,.2f}'.format(row[2]) if row[2] is not None else '—'}\n"
            f"- Open Positions: {row[3] or 0}"
        )

    def _md_open_positions(self) -> str:
        rows = self._conn.execute(
            """SELECT t.id, m.question, t.direction, t.quantity, t.executed_price
               FROM trades t
               JOIN markets m ON m.id = t.market_id
               WHERE t.status = 'open'
               ORDER BY t.opened_at DESC""",
        ).fetchall()
        if not rows:
            return "## Open Positions\nNone."
        lines = ["## Open Positions", "| ID | Market | Dir | Qty | Entry |", "|---|---|---|---|---|"]
        for r in rows:
            q = (r[1][:40] + "...") if len(r[1]) > 43 else r[1]
            lines.append(f"| {r[0]} | {q} | {r[2]} | {r[3]:.2f} | ${r[4]:.4f} |")
        return "\n".join(lines)

    def _md_overall_pnl(self) -> str:
        calc = MetricsCalculator(self._conn, self._settings)
        metrics = calc.compute()
        return (
            f"## Overall Performance\n"
            f"- Realized P&L: ${metrics.total_pnl:+,.4f}\n"
            f"- Win Rate: {metrics.win_rate:.1%}\n"
            f"- ROI: {metrics.roi:.2%}\n"
            f"- Pseudo Sharpe: {metrics.pseudo_sharpe:.3f}\n"
            f"- Max Drawdown: ${metrics.max_drawdown:,.2f}\n"
            f"- Kelly Adherence: {metrics.kelly_adherence:.1%}\n"
            f"- Total Trades: {metrics.total_trades}"
        )

    def _md_brier(self) -> str:
        rows = self._conn.execute(
            """SELECT model_name, period_type, brier_score, calibration_error, num_predictions
               FROM model_scores WHERE category = ?
               ORDER BY period_end DESC, model_name""",
            (OVERALL_CATEGORY_SENTINEL,),
        ).fetchall()
        if not rows:
            return "## Model Scores\nNo data."
        lines = [
            "## Model Scores",
            "| Model | Type | Brier | Cal Err | N |",
            "|---|---|---|---|---|",
        ]
        for model, ptype, brier, cal_err, n in rows:
            ce = f"{cal_err:.4f}" if cal_err is not None else "—"
            lines.append(f"| {model} | {ptype} | {brier:.4f} | {ce} | {n} |")
        return "\n".join(lines)

    def _md_top_trades(self) -> str:
        rows = self._conn.execute(
            """SELECT t.id, m.question, t.direction, t.pnl
               FROM trades t
               JOIN markets m ON m.id = t.market_id
               WHERE t.status = 'settled' AND t.pnl IS NOT NULL
               ORDER BY t.pnl DESC LIMIT 5""",
        ).fetchall()
        if not rows:
            return "## Top Trades\nNone."
        lines = ["## Top Trades", "| ID | Market | Dir | P&L |", "|---|---|---|---|"]
        for r in rows:
            q = (r[1][:40] + "...") if len(r[1]) > 43 else r[1]
            lines.append(f"| {r[0]} | {q} | {r[2]} | ${r[3]:+,.4f} |")
        return "\n".join(lines)
