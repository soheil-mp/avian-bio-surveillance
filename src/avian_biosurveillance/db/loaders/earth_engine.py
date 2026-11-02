"""Bulk upsert loader for Earth Engine environmental profiles."""

# mypy: disable-error-code=import-untyped

import logging
from collections.abc import Sequence

import psycopg2.extras  # type: ignore

from avian_biosurveillance.db.database import ConnectionPool, get_pool
from avian_biosurveillance.db.loaders._util import chunked
from avian_biosurveillance.db.models.earth_engine import (
    EarthEngineGridProfile,
    EarthEngineProfile,
)


logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 5_000

# Columns we INSERT.
_EEP_COLUMNS = tuple(EarthEngineProfile.column_names())

# Conflict on PK (station_id, observation_date)
_EEP_CONFLICT_COLS = ("station_id", "observation_date")
_EEP_UPDATE_COLS = tuple(c for c in _EEP_COLUMNS if c not in _EEP_CONFLICT_COLS)

_EEP_SQL = (
    f"INSERT INTO google_earth_engine_tbl ({', '.join(_EEP_COLUMNS)}) "
    f"VALUES %s "
    f"ON CONFLICT (station_id, observation_date) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _EEP_UPDATE_COLS)
)

_EEGP_COLUMNS = tuple(EarthEngineGridProfile.column_names())
_EEGP_CONFLICT_COLS = ("grid_cell_id", "observation_date")
_EEGP_UPDATE_COLS = tuple(c for c in _EEGP_COLUMNS if c not in _EEGP_CONFLICT_COLS)
_EEGP_SQL = (
    f"INSERT INTO google_earth_engine_tbl ({', '.join(_EEGP_COLUMNS)}) "
    f"VALUES %s "
    f"ON CONFLICT (grid_cell_id, observation_date) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _EEGP_UPDATE_COLS)
)


def upsert_earth_engine_profiles(
    records: Sequence[EarthEngineProfile],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Legacy station-level GEE upsert shim.

    Args:
        records: Dataclass instances to upsert.
        pool: Connection pool (defaults to module-level singleton).
        chunk_size: Rows per INSERT batch.

    Returns:
        Never returns; station-level persistence is retired.
    """
    msg = (
        "Station-level Earth Engine persistence is retired; "
        "use upsert_earth_engine_grid_profiles() against the canonical base-grid table."
    )
    raise RuntimeError(msg)


def upsert_earth_engine_grid_profiles(
    records: Sequence[EarthEngineGridProfile],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert Earth Engine grid profiles into ``google_earth_engine_tbl``."""
    if not records:
        return 0

    pool = pool or get_pool()
    total = 0

    for chunk in chunked(records, chunk_size):
        values = [tuple(getattr(r, c) for c in _EEGP_COLUMNS) for r in chunk]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _EEGP_SQL, values)
            total += len(values)
        logger.debug(
            "google_earth_engine_tbl: upserted %d rows (total %d)",
            len(values),
            total,
        )

    logger.info("google_earth_engine_tbl: upserted %d rows total", total)
    return total
