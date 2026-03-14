"""CLI entry point for the Lloyd performance dashboard."""
from __future__ import annotations

import argparse

from lloyd.config import get_settings
from lloyd.db import get_connection, init_db
from lloyd.postmortem.dashboard import Dashboard


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="lloyd.dashboard",
        description="Lloyd performance dashboard",
    )
    parser.add_argument(
        "--export",
        metavar="PATH",
        help="Export dashboard as markdown to the given file path",
    )
    args = parser.parse_args()

    settings = get_settings()
    conn = get_connection(settings.database_path)
    init_db(conn)

    try:
        dash = Dashboard(conn, settings)
        if args.export:
            dash.export_markdown(args.export)
            print(f"Dashboard exported to {args.export}")
        else:
            dash.render()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
