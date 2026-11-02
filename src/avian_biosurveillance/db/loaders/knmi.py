"""Bulk upsert loaders for KNMI data (weather + seismic).

Note: ``upsert_knmi_daily_summaries`` was removed in v6.
Daily summaries are now derived automatically via the
knmi_daily_cagg_tbl TimescaleDB continuous aggregate.
"""

import logging
from collections.abc import Sequence

import psycopg2.extras

from avian_biosurveillance.db.database import ConnectionPool, get_pool
from avian_biosurveillance.db.loaders._util import chunked
from avian_biosurveillance.db.models.knmi import KNMIWeatherObservation, SeismicEvent


logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 5_000


# =====================================================================
# KNMI Weather Observations
# =====================================================================

_KWO_COLUMNS = (
    "observed_at",
    "observation_date",
    "station_id",
    "station_name",
    "station_lat",
    "station_lon",
    "station_elevation_m",
    "temperature_c",
    "relative_humidity_pct",
    "wind_speed_ms",
    "wind_direction_deg",
    "pressure_hpa",
    "precipitation_mm",
    "solar_radiation_wm2",
    "cloud_cover_okta",
)

_KWO_CONFLICT_COLS = ("station_id", "observed_at")
_KWO_UPDATE_COLS = tuple(c for c in _KWO_COLUMNS if c not in _KWO_CONFLICT_COLS)

_KWO_SQL = (
    f"INSERT INTO knmi_tbl ({', '.join(_KWO_COLUMNS)}) "
    f"VALUES %s "
    f"ON CONFLICT (station_id, observed_at) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _KWO_UPDATE_COLS)
)


def upsert_knmi_observations(
    records: Sequence[KNMIWeatherObservation],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert KNMI 10-min observations into ``knmi_tbl``.

    Args:
        records: Dataclass instances to upsert.
        pool: Connection pool (defaults to module-level singleton).
        chunk_size: Rows per INSERT batch.

    Returns:
        Total number of rows upserted.
    """
    if not records:
        return 0

    pool = pool or get_pool()
    total = 0

    for chunk in chunked(records, chunk_size):
        values = [tuple(getattr(r, c) for c in _KWO_COLUMNS) for r in chunk]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _KWO_SQL, values)
            total += len(values)
        logger.debug("knmi_tbl: upserted %d rows (total %d)", len(values), total)

    logger.info("knmi_tbl: upserted %d rows total", total)
    return total


# =====================================================================
# Seismic Events
# =====================================================================

_SE_COLUMNS = (
    "event_id",
    "event_time",
    "latitude",
    "longitude",
    "depth_km",
    "magnitude",
    "magnitude_type",
    "description",
    "region",
    "attributes",
)
_SE_CONFLICT = ("event_id",)
_SE_UPDATE = tuple(c for c in _SE_COLUMNS if c not in _SE_CONFLICT)
_SE_SQL = (
    f"INSERT INTO seismic_tbl ({', '.join(_SE_COLUMNS)}) "
    f"VALUES %s "
    f"ON CONFLICT (event_id) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _SE_UPDATE)
)


def upsert_seismic_events(
    records: Sequence[SeismicEvent],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert seismic events into ``seismic_tbl``."""
    if not records:
        return 0
    pool = pool or get_pool()
    total = 0
    for chunk in chunked(records, chunk_size):
        values = [tuple(getattr(r, c) for c in _SE_COLUMNS) for r in chunk]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _SE_SQL, values)
            total += len(values)
    logger.info("seismic_tbl: upserted %d rows", total)
    return total
