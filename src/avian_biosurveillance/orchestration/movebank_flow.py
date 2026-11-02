"""Movebank GPS tracking ingestion flows.

Dagster tasks and flows for ingesting GPS tracking data from
`Movebank <https://www.movebank.org>`_.

``run_movebank_flow`` *(daily)*
    Collect the last *lookback_days* of GPS events for all
    target-species studies.

``run_movebank_repair_flow`` *(manual)*
    Collect GPS events for an explicit date range, useful for
    source-specific historical repair.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from avian_biosurveillance.ingestion.movebank import MovebankCollector
from avian_biosurveillance.orchestration.derived_aggregates_flow import (
    refresh_movebank_tracks_task,
)


logger = logging.getLogger(__name__)


def _movebank_run_name() -> str:
    """Generate run name: ``movebank-ingest/YYYY-MM-DD``."""
    return f"movebank-ingest/{date.today().isoformat()}"


def fetch_movebank_data(
    lookback_days: int = 7,
    target: str = "database",
) -> object:
    """Fetch recent Movebank GPS events for target-species studies.

    Delegates to
    :class:`~avian_biosurveillance.ingestion.movebank.MovebankCollector`
    which queries every study associated with the configured target
    species and ingests GPS fix events.

    Args:
        lookback_days: Number of days to look back for new events.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
    """
    today = date.today()
    start_date = today - timedelta(days=lookback_days)

    logger.info(
        "Starting Movebank ingestion (%s to %s).",
        start_date,
        today,
    )
    collector = MovebankCollector()
    stats = collector.collect_target_species_events(
        start_date=start_date,
        end_date=today,
        try_all_sensors=True,
        target=target,
    )
    logger.info("Movebank ingestion complete — %s", stats)
    return stats


def run_movebank_flow(
    lookback_days: int = 7,
    target: str = "database",
) -> None:
    """Dagster flow: daily Movebank GPS tracking ingestion."""
    fetch_movebank_data(
        lookback_days=lookback_days,
        target=target,
    )
    if target == "database":
        # Rebuild the per-individual decimated tracks consumed by
        # /api/migration-tracks, so the dashboard reflects the latest fixes.
        refresh_movebank_tracks_task()


# ==========================================================================
# Historic date-range backfill flow
# ==========================================================================


def _movebank_repair_run_name(
    start_date: str = "unknown",
    end_date: str | None = None,
    **_kwargs: object,
) -> str:
    """Generate run name: ``movebank-repair/YYYY-MM-DD..YYYY-MM-DD``."""
    end = end_date or start_date
    return f"movebank-repair/{start_date}..{end}"


def backfill_movebank_day(
    obs_date: date,
    all_sensors: bool = False,
    target: str = "database",
) -> None:
    """Fetch Movebank GPS events for a single calendar day."""
    logger.info(
        "Movebank repair day: %s (all_sensors=%s).",
        obs_date,
        all_sensors,
    )
    collector = MovebankCollector()
    stats = collector.collect_target_species_events(
        start_date=obs_date,
        end_date=obs_date,
        try_all_sensors=True,
        all_sensors=all_sensors,
        target=target,
    )
    logger.info("Movebank repair day complete — %s", stats)


def backfill_movebank_data(
    start_date: date,
    end_date: date,
    all_sensors: bool = False,
    target: str = "database",
) -> None:
    """Fetch Movebank GPS events for an explicit date range.

    Args:
        start_date: First date to ingest.
        end_date: Last date to ingest (inclusive).
        all_sensors: When *True*, collect from every available sensor
            type, not just the first match per study.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
    """
    logger.info(
        "Movebank repair: %s to %s (all_sensors=%s).",
        start_date,
        end_date,
        all_sensors,
    )
    collector = MovebankCollector()
    stats = collector.collect_target_species_events(
        start_date=start_date,
        end_date=end_date,
        try_all_sensors=True,
        all_sensors=all_sensors,
        target=target,
    )
    logger.info("Movebank repair complete — %s", stats)


def run_movebank_repair_flow(
    start_date: str,
    end_date: str | None = None,
    all_sensors: bool = False,
    target: str = "database",
) -> None:
    """Dagster flow: Movebank GPS tracking repair for a date range.

    Args:
        start_date: First date to ingest (``YYYY-MM-DD``).
        end_date: Last date to ingest (inclusive).  Defaults to today.
        all_sensors: Collect from every available sensor type per study.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date) if end_date else date.today()

    if end < start:
        msg = f"end_date ({end}) must be >= start_date ({start})"
        raise ValueError(msg)

    backfill_movebank_data(
        start_date=start,
        end_date=end,
        all_sensors=all_sensors,
        target=target,
    )
    if target == "database":
        # Mirror the daily flow so backfilled fixes are reflected in the
        # /api/migration-tracks payload.
        refresh_movebank_tracks_task()
