#!/usr/bin/env python3
"""Backfill missing Earth Engine population fields in ``google_earth_engine_tbl``.

This script targets the remaining historical gap left after the Earth Engine
 ingestion audit: rows where ``worldpop_count`` is still null. It intentionally
queries only the ``population`` section from Google Earth Engine and updates
only the population-related columns in the database.

Why this exists:
- re-running full Earth Engine profiles for every missing row would be slow and
  needlessly re-query dozens of unrelated datasets;
- the collector now blocks partial DB writes (correctly), so population-only
  repairs need a dedicated merge/update path.

Usage::

    python scripts/repair/backfill_earth_engine_population.py --dry-run
    python scripts/repair/backfill_earth_engine_population.py --year 2024
    python scripts/repair/backfill_earth_engine_population.py
"""

from __future__ import annotations

import argparse
import sys
import threading
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psycopg2.extras


_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "src"))

from avian_biosurveillance.db.database import get_pool
from avian_biosurveillance.ingestion.earth_engine import EarthEngineProfiler


@dataclass(frozen=True, slots=True)
class PopulationBackfillTarget:
    """One station-year group that needs population fields backfilled."""

    station_id: str
    observation_year: int
    sample_date: date
    lat: float
    lon: float
    rows_to_update: int


_THREAD_LOCAL = threading.local()


def _load_targets(
    *, year: int | None = None, limit: int | None = None
) -> list[PopulationBackfillTarget]:
    """Load station-year groups whose ``worldpop_count`` is missing."""
    pool = get_pool()
    where = ["e.worldpop_count IS NULL"]
    params: list[object] = []

    if year is not None:
        where.append("EXTRACT(YEAR FROM e.observation_date)::int = %s")
        params.append(year)

    limit_sql = ""
    if limit is not None:
        limit_sql = "LIMIT %s"
        params.append(limit)

    sql = f"""
        SELECT
            e.station_id,
            EXTRACT(YEAR FROM e.observation_date)::int AS observation_year,
            MIN(e.observation_date) AS sample_date,
            MAX(COALESCE(e.lat, s.lat)) AS lat,
            MAX(COALESCE(e.lon, s.lon)) AS lon,
            COUNT(*)::int AS rows_to_update
        FROM google_earth_engine_tbl e
        LEFT JOIN birdweather_stations_tbl s USING (station_id)
        WHERE {" AND ".join(where)}
        GROUP BY e.station_id, observation_year
        ORDER BY observation_year, e.station_id
        {limit_sql}
    """

    with pool.cursor() as cur:
        cur.execute(sql, params)
        rows = cur.fetchall()

    return [
        PopulationBackfillTarget(
            station_id=str(station_id),
            observation_year=int(observation_year),
            sample_date=sample_date,
            lat=float(lat),
            lon=float(lon),
            rows_to_update=int(rows_to_update),
        )
        for station_id, observation_year, sample_date, lat, lon, rows_to_update in rows
        if lat is not None and lon is not None
    ]


def _get_thread_profiler(*, high_volume: bool) -> EarthEngineProfiler:
    """Return one Earth Engine profiler per worker thread."""
    profiler = getattr(_THREAD_LOCAL, "profiler", None)
    if profiler is None:
        profiler = EarthEngineProfiler(high_volume=high_volume)
        _THREAD_LOCAL.profiler = profiler
    return profiler


def _query_one_target(
    target: PopulationBackfillTarget, *, high_volume: bool
) -> tuple[float | None, float | None, str, int]:
    """Query Earth Engine for one station-year target."""
    profiler = _get_thread_profiler(high_volume=high_volume)
    result = profiler.get_population(
        target.lat,
        target.lon,
        target.sample_date.isoformat(),
    )
    return (
        result.get("worldpop_count"),
        result.get("gpw_density_per_km2"),
        target.station_id,
        target.observation_year,
    )


def _query_population(
    targets: Sequence[PopulationBackfillTarget],
    *,
    high_volume: bool,
    workers: int,
    progress_every: int = 100,
) -> tuple[list[tuple[float | None, float | None, str, int]], int]:
    """Query Earth Engine population data for the target station-year groups."""
    updates: list[tuple[float | None, float | None, str, int]] = []
    failures = 0

    total = len(targets)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures = {
            executor.submit(_query_one_target, target, high_volume=high_volume): target
            for target in targets
        }
        for index, future in enumerate(as_completed(futures), start=1):
            worldpop_count, gpw_density, station_id, observation_year = future.result()
            if worldpop_count is None and gpw_density is None:
                failures += 1
            else:
                updates.append(
                    (worldpop_count, gpw_density, station_id, observation_year)
                )

            if index == 1 or index % progress_every == 0 or index == total:
                print(
                    "backfill_earth_engine_population: "
                    f"queried {index}/{total} station-year target(s)"
                )

    return updates, failures


def _apply_updates(
    updates: Sequence[tuple[float | None, float | None, str, int]],
) -> int:
    """Apply population updates to all matching rows in the table."""
    if not updates:
        return 0

    pool = get_pool()

    with pool.connection() as conn:
        with conn.cursor() as cur:
            psycopg2.extras.execute_batch(
                cur,
                """
                UPDATE google_earth_engine_tbl
                SET worldpop_count = COALESCE(%s, worldpop_count),
                    gpw_density_per_km2 = COALESCE(%s, gpw_density_per_km2)
                WHERE station_id = %s
                  AND EXTRACT(YEAR FROM observation_date)::int = %s
                  AND worldpop_count IS NULL
                """,
                updates,
                page_size=250,
            )
        conn.commit()

    with pool.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM google_earth_engine_tbl
            WHERE worldpop_count IS NOT NULL
            """
        )
        return int(cur.fetchone()[0])


def run(
    *,
    year: int | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    high_volume: bool = False,
    workers: int = 4,
    progress_every: int = 100,
) -> int:
    """Backfill missing population data and return the number of target groups."""
    targets = _load_targets(year=year, limit=limit)
    rows = sum(target.rows_to_update for target in targets)

    if not targets:
        print("backfill_earth_engine_population: no missing station-year targets found")
        return 0

    print(
        "backfill_earth_engine_population: "
        f"{len(targets)} station-year target(s), {rows} table row(s) affected"
    )
    if dry_run:
        return len(targets)

    updates, failures = _query_population(
        targets,
        high_volume=high_volume,
        workers=workers,
        progress_every=progress_every,
    )

    _apply_updates(updates)
    print(
        "backfill_earth_engine_population: "
        f"prepared {len(updates)} update(s), {failures} target(s) returned no data"
    )
    return len(updates)


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(
        description="Backfill missing Earth Engine population fields.",
    )
    parser.add_argument(
        "--year",
        type=int,
        default=None,
        help="Restrict to a specific observation year.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Restrict the number of station-year targets processed.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report missing scope without querying Earth Engine or updating DB.",
    )
    parser.add_argument(
        "--high-volume",
        action="store_true",
        help="Use the Earth Engine high-volume endpoint.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Number of parallel Earth Engine worker threads.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=100,
        help="Print progress after this many station-year queries.",
    )
    args = parser.parse_args()

    run(
        year=args.year,
        limit=args.limit,
        dry_run=args.dry_run,
        high_volume=args.high_volume,
        workers=max(1, args.workers),
        progress_every=max(1, args.progress_every),
    )


if __name__ == "__main__":
    main()
