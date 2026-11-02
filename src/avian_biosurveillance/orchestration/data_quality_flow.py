"""Data Quality Audit Flow.

Orchestrates the daily `ISO 25012`_ quality audit across all source
tables:

- ``birdweather_tbl``
- ``knmi_tbl``
- ``sovon_tbl``
- ``ebird_tbl``
- ``movebank_tbl``
- ``google_earth_engine_grid_tbl``
- ``seismic_tbl``

Results are persisted to ``data_quality_tbl`` for dashboard consumption
and trend tracking.

.. _ISO 25012: https://www.iso.org/standard/35749.html
"""

from __future__ import annotations

import logging
from datetime import date

from avian_biosurveillance.data.quality import run_audit, store_results
from avian_biosurveillance.db.database import ConnectionPool


logger = logging.getLogger(__name__)


def _quality_run_name() -> str:
    """Generate run name: ``quality-audit/YYYY-MM-DD``."""
    return f"quality-audit/{date.today().isoformat()}"


# ==========================================================
# Data Quality Audit Flow
# ==========================================================


def run_quality_audit_task() -> None:
    """Execute the full quality audit and persist results.

    Opens a short-lived connection pool, runs the audit (40+ metrics
    across six ISO 25012 dimensions), and writes summary + per-station
    metric rows into ``data_quality_tbl``.

    The audit covers completeness, consistency, accuracy, timeliness,
    uniqueness, and validity for every configured source table.
    """
    logger.info("Starting data-quality audit across all tables.")
    pool = ConnectionPool(min_conn=1, max_conn=3)
    try:
        report, station_metrics = run_audit(pool)

        run_id = store_results(pool, report, station_metrics)

        n_fail = sum(1 for m in report.metrics if m.status == "FAIL")
        n_warn = sum(1 for m in report.metrics if m.status == "WARN")

        logger.info(
            "Quality audit complete — run #%d: %d metrics, "
            "%d stations, %d failures, %d warnings.",
            run_id,
            len(report.metrics),
            len(station_metrics),
            n_fail,
            n_warn,
        )
    finally:
        pool.close()


def run_quality_flow() -> None:
    """Dagster flow: daily ISO 25012 data-quality audit.

    Wraps :func:`run_quality_audit_task` in a Dagster flow for
    standalone or ad-hoc execution.  When running as part of the daily
    pipeline, the task is called directly instead.
    """
    run_quality_audit_task()
