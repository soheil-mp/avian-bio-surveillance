#!/usr/bin/env python3
"""Recreate and run ``refresh_station_summary()`` from the canonical SQL schema.

Useful as a manual repair helper when the database function definition has
changed and you want to re-register the latest SQL implementation before
refreshing station summary rows.

Usage::

    python scripts/repair/refresh_station_summary.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from avian_biosurveillance.db.database import ConnectionPool


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))


SQL_FILE = _REPO_ROOT / "db" / "routines" / "010_refresh_station_summary.sql"


def _extract_refresh_function(sql_text: str) -> str:
    """Extract the latest ``refresh_station_summary()`` function definition."""
    start = sql_text.index("CREATE OR REPLACE FUNCTION refresh_station_summary()")
    end = sql_text.index("$$;", start) + len("$$;")
    return sql_text[start:end]


def main() -> int:
    """Re-register and execute the station summary refresh function."""
    pool = ConnectionPool(min_conn=1, max_conn=2)
    try:
        print(f"Re-creating refresh_station_summary() from {SQL_FILE.name} ...")
        sql_text = SQL_FILE.read_text(encoding="utf-8")
        func_sql = _extract_refresh_function(sql_text)
        with pool.cursor() as cur:
            cur.execute(func_sql)
        print("Function re-created.")

        print("Running refresh_station_summary() ...")
        with pool.cursor() as cur:
            cur.execute("SELECT refresh_station_summary()")
        print("station_summary refreshed.")
        return 0
    finally:
        pool.close()


if __name__ == "__main__":
    raise SystemExit(main())
