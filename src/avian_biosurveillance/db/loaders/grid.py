"""Bulk upsert loader for canonical grid cells."""

from __future__ import annotations

import logging
from collections.abc import Sequence

import psycopg2.extras  # type: ignore

from avian_biosurveillance.db.database import ConnectionPool, get_pool
from avian_biosurveillance.db.loaders._util import chunked
from avian_biosurveillance.db.models.grid import GridCell


logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 5_000

_GRID_COLUMNS = tuple(GridCell.column_names())
_GRID_CONFLICT_COLS = ("grid_cell_id",)
_GRID_UPDATE_COLS = tuple(
    col for col in _GRID_COLUMNS if col not in _GRID_CONFLICT_COLS
)
_GRID_SQL = (
    f"INSERT INTO grid_cells_tbl ({', '.join(_GRID_COLUMNS)}) "
    f"VALUES %s "
    f"ON CONFLICT (grid_cell_id) DO UPDATE SET "
    + ", ".join(f"{col} = EXCLUDED.{col}" for col in _GRID_UPDATE_COLS)
)


def upsert_grid_cells(
    records: Sequence[GridCell],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert canonical grid cells into ``grid_cells_tbl``."""
    if not records:
        return 0

    pool = pool or get_pool()
    total = 0

    for chunk in chunked(records, chunk_size):
        values = [
            tuple(getattr(record, col) for col in _GRID_COLUMNS) for record in chunk
        ]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _GRID_SQL, values)
            total += len(values)
        logger.debug("grid_cells_tbl: upserted %d rows (total %d)", len(values), total)

    logger.info("grid_cells_tbl: upserted %d rows total", total)
    return total
