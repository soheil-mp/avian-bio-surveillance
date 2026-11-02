"""Bulk upsert loader for Movebank event data."""

import logging
from collections.abc import Sequence

import psycopg2.extras

from avian_biosurveillance.db.database import ConnectionPool, get_pool
from avian_biosurveillance.db.loaders._util import chunked
from avian_biosurveillance.db.models.movebank import MovebankEvent


logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 5_000

# =====================================================================
# Movebank Events
# =====================================================================

# Columns we INSERT. Derived dynamically from the dataclass so schema changes
# stay in sync.
_MBE_COLUMNS = tuple(MovebankEvent.column_names())

# Composite PK: (event_id, study_id, timestamp)
_MBE_CONFLICT_COLS = ("event_id", "study_id", "timestamp")
_MBE_UPDATE_COLS = tuple(c for c in _MBE_COLUMNS if c not in _MBE_CONFLICT_COLS)

_MBE_SQL = (
    f"INSERT INTO movebank_tbl ({', '.join(_MBE_COLUMNS)}) "
    f"VALUES %s "
    f"ON CONFLICT (event_id, study_id, timestamp) DO UPDATE SET "
    + ", ".join(f"{c} = EXCLUDED.{c}" for c in _MBE_UPDATE_COLS)
)


def upsert_movebank_events(
    records: Sequence[MovebankEvent],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert Movebank events into ``movebank_tbl``.

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
        values = [tuple(getattr(r, c) for c in _MBE_COLUMNS) for r in chunk]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _MBE_SQL, values)
            total += len(values)
        logger.debug(
            "movebank_tbl: upserted %d rows (total %d)",
            len(values),
            total,
        )

    logger.info("movebank_tbl: upserted %d rows total", total)
    return total
