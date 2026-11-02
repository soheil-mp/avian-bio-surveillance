"""Station Summary Refresh Flow.

Calls the ``refresh_station_summary()`` PL/pgSQL function in
TimescaleDB to recompute per-station aggregates:

- Detection counts and unique observation days
- Mean / max confidence scores
- Temporal coverage (first / last detection)
- Inline data-quality scores

It then calls ``refresh_station_gee_enrichment()`` to materialise the static
station -> grid-cell mapping and per-cell Google Earth Engine features
(90-day NDVI mean, elevation mean, latest ESA WorldCover label) onto the same
table, so the /api/stations dashboard route is a single-table read.

Results are written back into ``birdweather_stations_tbl``.
"""

from __future__ import annotations

import logging
import os
from datetime import date
from urllib.parse import quote_plus

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.db.database import ConnectionPool


load_runtime_env()

logger = logging.getLogger(__name__)

_STATION_SUMMARY_DATABASE_URL_ENV = "STATION_SUMMARY_DATABASE_URL"
_STATION_SUMMARY_DB_USER_ENV = "STATION_SUMMARY_DB_USER"
_STATION_SUMMARY_DB_PASSWORD_ENV = "STATION_SUMMARY_DB_PASSWORD"
_STATION_SUMMARY_DB_HOST_ENV = "STATION_SUMMARY_DB_HOST"
_STATION_SUMMARY_DB_PORT_ENV = "STATION_SUMMARY_DB_PORT"
_STATION_SUMMARY_DB_NAME_ENV = "STATION_SUMMARY_DB_NAME"


def _station_summary_run_name() -> str:
    """Generate run name: ``station-summary-refresh/YYYY-MM-DD``."""
    return f"station-summary-refresh/{date.today().isoformat()}"


def _station_summary_dsn() -> str | None:
    """Build a privileged DSN for station-summary maintenance if configured."""
    dsn = os.environ.get(_STATION_SUMMARY_DATABASE_URL_ENV)
    if dsn:
        return dsn

    user = os.environ.get(_STATION_SUMMARY_DB_USER_ENV)
    password = os.environ.get(_STATION_SUMMARY_DB_PASSWORD_ENV)
    if not user or not password:
        return None

    host = os.environ.get(_STATION_SUMMARY_DB_HOST_ENV, "timescaledb")
    port = os.environ.get(_STATION_SUMMARY_DB_PORT_ENV, "5432")
    dbname = os.environ.get(
        _STATION_SUMMARY_DB_NAME_ENV,
        os.environ.get("POSTGRES_DB", "avian_biosurveillance"),
    )
    return (
        f"postgresql://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{dbname}"
    )


def _station_summary_pool() -> ConnectionPool:
    """Build the pool used for privileged station-summary maintenance.

    ``refresh_continuous_aggregate`` requires ownership of the continuous
    aggregate view. In production this view is owned by the database bootstrap
    role, so the worker needs a dedicated owner-level DSN for this task.
    """
    dsn = _station_summary_dsn()
    if dsn:
        logger.info(
            "Using privileged maintenance connection for station summary refresh.",
        )
        return ConnectionPool(dsn=dsn, min_conn=1, max_conn=2)
    return ConnectionPool(min_conn=1, max_conn=2)


# ==========================================================
# Station Summary Refresh Flow
# ==========================================================


def refresh_station_summary_task() -> None:
    """Refresh the continuous aggregate then recompute station summaries.

    First refreshes ``birdweather_daily_cagg_tbl`` so the latest
    detections are visible, then calls ``refresh_station_summary()``
    to rebuild ``birdweather_stations_tbl``.
    """
    pool = _station_summary_pool()
    try:
        with pool.connection() as conn:
            previous_autocommit = conn.autocommit
            conn.autocommit = True
            try:
                with conn.cursor() as cur:
                    cur.execute(
                        "CALL refresh_continuous_aggregate("
                        "'birdweather_daily_cagg_tbl', NULL, NULL)"
                    )
                    logger.info("birdweather_daily_cagg_tbl refreshed.")
                    cur.execute("SELECT refresh_station_summary()")
                    logger.info("birdweather_stations_tbl refreshed successfully.")
                    cur.execute("SELECT refresh_station_gee_enrichment()")
                    logger.info(
                        "birdweather_stations_tbl GEE enrichment refreshed."
                    )
            finally:
                conn.autocommit = previous_autocommit
    finally:
        pool.close()


def run_station_summary_flow() -> None:
    """Dagster flow: refresh ``birdweather_stations_tbl`` aggregates.

    Wraps :func:`refresh_station_summary_task` in a standalone Dagster
    flow for ad-hoc execution.  Separating this from the ingestion
    pipeline allows the refresh to run independently when needed.
    """
    refresh_station_summary_task()
