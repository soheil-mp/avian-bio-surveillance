"""Bulk upsert loader for eBird observation data."""

import logging
from collections.abc import Sequence

import psycopg2.extras

from avian_biosurveillance.db.database import ConnectionPool, get_pool
from avian_biosurveillance.db.loaders._util import chunked
from avian_biosurveillance.db.models.ebird import EBirdObservation


logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 5_000

# =====================================================================
# eBird Observations
# =====================================================================

# Columns we INSERT.
_EBO_COLUMNS = (
    "sub_id",
    "species_code",
    "obs_dt",
    "observation_date",
    "common_name",
    "scientific_name",
    "how_many",
    "presence_noted",
    "loc_id",
    "loc_name",
    "lat",
    "lng",
    "location_private",
    "obs_valid",
    "obs_reviewed",
    "subnational2_code",
    "subnational2_name",
    "subnational1_code",
    "subnational1_name",
    "country_code",
    "country_name",
    "user_display_name",
    "checklist_id",
    "has_comments",
    "has_rich_media",
    "num_species_all_time",
    "is_hotspot",
)

# Composite PK: (sub_id, species_code, obs_dt)
_EBO_CONFLICT_COLS = ("sub_id", "species_code", "obs_dt")
_EBO_UPDATE_COLS = tuple(c for c in _EBO_COLUMNS if c not in _EBO_CONFLICT_COLS)

_EBO_SQL = (
    f"INSERT INTO ebird_tbl ({', '.join(_EBO_COLUMNS)}) "
    f"VALUES %s "
    f"ON CONFLICT (sub_id, species_code, obs_dt) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _EBO_UPDATE_COLS)
)


def upsert_ebird_observations(
    records: Sequence[EBirdObservation],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert eBird observations into ``ebird_tbl``.

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
        values = [tuple(getattr(r, c) for c in _EBO_COLUMNS) for r in chunk]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _EBO_SQL, values)
            total += len(values)
        logger.debug(
            "ebird_tbl: upserted %d rows (total %d)",
            len(values),
            total,
        )

    logger.info("ebird_tbl: upserted %d rows total", total)
    return total
