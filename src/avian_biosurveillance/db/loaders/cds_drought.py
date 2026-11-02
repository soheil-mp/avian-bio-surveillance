"""Bulk upsert loader for CDS drought observations."""

# mypy: disable-error-code=import-untyped

from __future__ import annotations

import logging
from collections.abc import Sequence

import psycopg2.extras  # type: ignore

from avian_biosurveillance.db.database import ConnectionPool, get_pool
from avian_biosurveillance.db.loaders._util import chunked
from avian_biosurveillance.db.models.cds_drought import CDSDroughtObservation


logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 5_000

_CDS_COLUMNS = tuple(CDSDroughtObservation.column_names())
_CDS_CONFLICT_COLS = (
    "grid_cell_id",
    "observation_month",
    "dataset_type",
    "product_type",
    "ensemble_member",
    "data_version",
)
_CDS_UPDATE_COLS = tuple(col for col in _CDS_COLUMNS if col not in _CDS_CONFLICT_COLS)

_CDS_SQL = (
    f"INSERT INTO cds_drought_tbl ({', '.join(_CDS_COLUMNS)}) "  # noqa: S608
    f"VALUES %s "
    f"ON CONFLICT ({', '.join(_CDS_CONFLICT_COLS)}) DO UPDATE SET "
    + ", ".join(f"{col} = EXCLUDED.{col}" for col in _CDS_UPDATE_COLS)
)


def upsert_cds_drought_observations(
    records: Sequence[CDSDroughtObservation],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert CDS drought observations into ``cds_drought_tbl``."""
    if not records:
        return 0

    pool = pool or get_pool()
    total = 0

    for chunk in chunked(records, chunk_size):
        values = [
            tuple(getattr(record, column) for column in _CDS_COLUMNS)
            for record in chunk
        ]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _CDS_SQL, values)
            total += len(values)
        logger.debug(
            "cds_drought_tbl: upserted %d rows (total %d)",
            len(values),
            total,
        )

    logger.info("cds_drought_tbl: upserted %d rows total", total)
    return total
