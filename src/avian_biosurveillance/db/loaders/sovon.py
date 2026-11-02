"""Bulk upsert loaders for Sovon mortality records."""

import logging
from collections.abc import Sequence

import psycopg2.extras

from avian_biosurveillance.db.database import ConnectionPool, get_pool
from avian_biosurveillance.db.loaders._util import chunked


logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 5_000

# =====================================================================
# 4. Sovon Mortality
# =====================================================================

# Note: sovon_tbl is not backed by a dataclass in models.py
# (it comes from vision-LLM extraction output), so we accept dicts.

_SVM_COLUMNS = (
    "species_english",
    "species_dutch",
    "sovon_species_id",
    "year",
    "month",
    "dead_bird_count",
    "n_total",
    "source_image",
)

_SVM_SQL = (
    f"INSERT INTO sovon_tbl ({', '.join(_SVM_COLUMNS)}) "
    f"VALUES %s "
    f"ON CONFLICT ON CONSTRAINT uq_sovon_species_year_month DO UPDATE SET "
    + ", ".join(
        f"{c} = EXCLUDED.{c}"
        for c in _SVM_COLUMNS
        if c not in ("species_english", "year", "month")
    )
)


def upsert_sovon_mortality(
    records: Sequence[dict[str, object]],
    *,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Upsert Sovon mortality records into ``sovon_tbl``.

    Each dict must contain keys matching :data:`_SVM_COLUMNS`.
    Missing keys default to ``None``.

    Args:
        records: Dicts with mortality data from vision-LLM extraction.
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
        values = [tuple(r.get(c) for c in _SVM_COLUMNS) for r in chunk]
        with pool.cursor() as cur:
            psycopg2.extras.execute_values(cur, _SVM_SQL, values)
            total += len(values)
        logger.debug("sovon_tbl: upserted %d rows (total %d)", len(values), total)

    logger.info("sovon_tbl: upserted %d rows total", total)
    return total
