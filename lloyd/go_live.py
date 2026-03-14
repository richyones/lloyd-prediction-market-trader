"""CLI entry point for the go-live evaluation check."""
from __future__ import annotations

from lloyd.config import get_settings
from lloyd.db import get_connection, init_db
from lloyd.postmortem.go_live_check import GoLiveChecker


def main() -> None:
    settings = get_settings()
    conn = get_connection(settings.database_path)
    init_db(conn)

    try:
        checker = GoLiveChecker(conn, settings)
        result = checker.run()
    finally:
        conn.close()

    from rich.console import Console
    from rich.table import Table

    console = Console()

    table = Table(title="Go-Live Evaluation")
    table.add_column("Criterion")
    table.add_column("Pass?", justify="center")
    table.add_column("Value", justify="right")
    table.add_column("Threshold", justify="right")
    table.add_column("Detail")

    for c in result.criteria:
        icon = "[green]✓[/green]" if c.passed else "[red]✗[/red]"
        val = f"{c.value:.4f}" if c.value is not None else "—"
        thr = f"{c.threshold:.4f}" if c.threshold is not None else "—"
        table.add_row(c.name, icon, val, thr, c.detail)

    console.print(table)

    verdict = "[bold green]GO[/bold green]" if result.go else "[bold red]NO-GO[/bold red]"
    console.print(f"\nVerdict: {verdict}")

    if result.weakest:
        console.print("\nWeakest criteria:")
        for c in result.weakest:
            console.print(f"  - {c.name}: {c.value:.4f} (threshold: {c.threshold:.4f})")


if __name__ == "__main__":
    main()
