"""KNMI weather and seismic data ingestion flows.

Three Dagster flows are provided:

``run_knmi_flow`` *(daily)*
    Ingests the last 26 hours of 10-minute observations from all KNMI
    EDR stations (24 h + 2 h overlap for late-arriving data).

``run_knmi_repair_flow`` *(manual)*
    Ingests a specific date range day-by-day, writing one
    ``data/raw/knmi/<YYYY-MM-DD>/`` directory per day with per-station
    JSON files and a ``daily_unified.json`` summary.  Idempotent:
    already-ingested days are skipped unless ``force=True``.

``run_seismic_flow`` *(daily)*
    Fetches recent earthquake events from the KNMI FDSN Event API and
    persists them to ``seismic_tbl``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, time, timedelta
from datetime import date as datetime_date
from typing import Any

from avian_biosurveillance.ingestion.knmi import KNMIConfig, KNMIIngestionManager


logger = logging.getLogger(__name__)


def _knmi_run_name() -> str:
    """Generate run name: ``knmi-ingest/YYYY-MM-DD``."""
    return f"knmi-ingest/{datetime_date.today().isoformat()}"


def _seismic_run_name() -> str:
    """Generate run name: ``seismic-ingest/YYYY-MM-DD``."""
    return f"seismic-ingest/{datetime_date.today().isoformat()}"


# ==========================================================================
# Daily live-ingestion flow
# ==========================================================================


def fetch_knmi_data(
    target: str = "database",
    hours_back: int = 26,
) -> None:
    """Fetch the last ``hours_back`` hours of KNMI 10-minute observations.

    Uses the EDR API for all stations in the Netherlands.  Defaults to 26
    hours for daily runs (24 h + 2 h overlap to capture late-arriving data).

    Args:
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
        hours_back: Hours of history to cover (default: 26 for daily overlap).
    """
    logger.info("Starting KNMI daily ingestion (last %d hours).", hours_back)
    config = KNMIConfig()
    ingestor = KNMIIngestionManager(config=config)

    results = ingestor.ingest_meteorological_observations(
        hours_back=hours_back,
        target=target,
    )

    n_locs = len(results.get("edr_data", []))

    if target == "database":
        ingestor.persist_observations(results)
        logger.info(
            "KNMI daily ingestion complete — %d locations persisted to DB.",
            n_locs,
        )
    else:
        logger.info(
            "KNMI daily ingestion complete — %d locations saved locally.",
            n_locs,
        )


def run_knmi_flow(
    target: str = "database",
    hours_back: int = 26,
) -> None:
    """Dagster flow: daily KNMI 10-minute observation ingestion.

    Runs once per day and fetches the last 26 hours (24 h + 2 h overlap)
    to capture late-arriving observations.
    """
    fetch_knmi_data(
        target=target,
        hours_back=hours_back,
    )


# ==========================================================================
# Date-range backfill flow
# ==========================================================================


def backfill_knmi_day(
    date: datetime_date,
    target: str = "local",
    force: bool = False,
    ingestor: KNMIIngestionManager | None = None,
) -> dict:
    """Ingest one calendar day of KNMI observations.

    Writes to ``data/raw/knmi/<YYYY-MM-DD>/`` with per-station JSON
    files and a ``daily_unified.json`` summary.

    Args:
        date: The calendar date to ingest.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
        force: Re-download even if the day already has data on disk.

    Returns:
        Dictionary with keys ``skipped``, ``total_observations``, and
        ``station_count`` describing the ingestion outcome.
    """
    manager = ingestor
    if manager is None:
        config = KNMIConfig()
        manager = KNMIIngestionManager(config=config)

    result = manager.ingest_day(
        date=date,
        target=target,
        force=force,
    )
    action = "skipped" if result["skipped"] else "ingested"
    logger.info(
        "[%s] %s — %d observations across %d stations.",
        date,
        action,
        result["total_observations"],
        result["station_count"],
    )
    return result


def _knmi_repair_run_name(
    start_date: str = "unknown",
    end_date: str | None = None,
    **_kwargs: object,
) -> str:
    """Generate run name: ``knmi-repair/YYYY-MM-DD..YYYY-MM-DD``."""
    end = end_date or start_date
    return f"knmi-repair/{start_date}..{end}"


def run_knmi_repair_flow(
    start_date: str,
    end_date: str | None = None,
    target: str = "local",
    force: bool = False,
) -> dict:
    """Dagster flow: repair KNMI observations for a date range.

    Each day is submitted as an independent task so Dagster can track
    per-day success/failure independently.

    Args:
        start_date: First date to ingest in ``YYYY-MM-DD`` format.
        end_date: Last date to ingest (inclusive). Defaults to ``start_date``
            (single day) when not provided.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
        force: Re-download days that already have a ``daily_unified.json``.

    Returns:
        Summary dict with ``days_total``, ``days_ingested``, ``days_skipped``,
        ``days_failed``, and ``total_observations``.

    Example usage from the Dagster UI or Python::

        from avian_biosurveillance.orchestration.knmi_flow import run_knmi_repair_flow

        run_knmi_repair_flow(start_date="2024-01-01", end_date="2024-01-31")
    """
    start = datetime_date.fromisoformat(start_date)
    end = datetime_date.fromisoformat(end_date) if end_date else start

    if end < start:
        raise ValueError(f"end_date ({end}) must be >= start_date ({start})")

    total_days = (end - start).days + 1
    logger.info(
        "KNMI repair: %d day(s) from %s to %s (target=%s, force=%s)",
        total_days,
        start,
        end,
        target,
        force,
    )

    config = KNMIConfig()
    ingestor = KNMIIngestionManager(config=config)

    # Run one day at a time, newest first, so the most recent data lands first.
    days_ingested = days_skipped = days_failed = total_obs = 0
    per_day: list[dict] = []
    current = end
    while current >= start:
        date_str = current.isoformat()
        try:
            result = backfill_knmi_day(
                date=current,
                target=target,
                force=force,
                ingestor=ingestor,
            )
            per_day.append(result)
            if result.get("skipped"):
                days_skipped += 1
            else:
                days_ingested += 1
                total_obs += result.get("total_observations", 0)
        except Exception as exc:
            logger.error("[%s] Task failed: %s", date_str, exc)
            per_day.append({"date": date_str, "failed": True, "error": str(exc)})
            days_failed += 1
        current -= timedelta(days=1)

    summary = {
        "start_date": start_date,
        "end_date": end_date or start_date,
        "days_total": total_days,
        "days_ingested": days_ingested,
        "days_skipped": days_skipped,
        "days_failed": days_failed,
        "total_observations": total_obs,
    }
    logger.info(
        "Repair complete: %d ingested, %d skipped, %d failed. %d total obs.",
        days_ingested,
        days_skipped,
        days_failed,
        total_obs,
    )
    if days_failed:
        msg = f"KNMI repair failed for {days_failed} day(s)."
        raise RuntimeError(msg)
    return summary


# ==========================================================================
# Seismic event ingestion flow
# ==========================================================================


def fetch_seismic_events(
    days_back: int = 7,
    target: str = "database",
) -> int:
    """Fetch recent earthquake events from the KNMI FDSN Event API.

    Converts ingestion-layer ``SeismicEvent`` objects to DB-model
    ``SeismicEvent`` instances and upserts them into ``seismic_tbl``.

    Args:
        days_back: Number of days of history to retrieve.
        target: ``"local"`` (log only) or ``"database"`` (upsert to DB).

    Returns:
        Number of events upserted.
    """
    from avian_biosurveillance.ingestion.knmi import get_recent_earthquakes

    logger.info("Fetching seismic events from the last %d days.", days_back)
    events = get_recent_earthquakes(
        days=days_back,
        save_result=target == "local",
    )

    if not events:
        logger.info("No seismic events found.")
        return 0

    logger.info("Fetched %d seismic event(s).", len(events))

    return _persist_seismic_events(events, target=target)


def fetch_seismic_events_for_range(
    start_date: datetime_date,
    end_date: datetime_date,
    target: str = "database",
    min_magnitude: float = 0.0,
) -> int:
    """Fetch KNMI FDSN seismic events for an inclusive calendar date range."""
    from avian_biosurveillance.ingestion.knmi import create_ingestion_manager

    if end_date < start_date:
        msg = f"end_date ({end_date}) must be >= start_date ({start_date})"
        raise ValueError(msg)

    start_time = datetime.combine(start_date, time.min, tzinfo=UTC)
    end_time = datetime.combine(end_date, time.max, tzinfo=UTC)
    logger.info("Fetching seismic events from %s to %s.", start_time, end_time)
    manager = create_ingestion_manager()
    events = manager.fdsn_event.query(
        starttime=start_time,
        endtime=end_time,
        minmagnitude=min_magnitude,
        format="text",
        save_result=target == "local",
    )
    if not isinstance(events, list):
        msg = f"Unexpected seismic response type: {type(events).__name__}"
        raise TypeError(msg)
    if not events:
        logger.info("No seismic events found.")
        return 0
    logger.info("Fetched %d seismic event(s).", len(events))
    return _persist_seismic_events(events, target=target)


def _persist_seismic_events(events: list[Any], *, target: str) -> int:
    """Persist parsed seismic events when the target is the database."""
    from avian_biosurveillance.db.loaders import upsert_seismic_events
    from avian_biosurveillance.db.models import SeismicEvent as SeismicEventModel

    if target != "database":
        logger.info("Seismic: target=%s — skipping DB upsert.", target)
        return len(events)

    models = [
        SeismicEventModel.from_api(
            {
                "event_id": e.event_id,
                "time": e.time.isoformat() if e.time else None,
                "latitude": e.latitude,
                "longitude": e.longitude,
                "depth": e.depth,
                "magnitude": e.magnitude,
                "magnitude_type": e.magnitude_type,
                "description": e.description,
            }
        )
        for e in events
    ]
    n = upsert_seismic_events(models)
    logger.info("Seismic ingestion complete — %d events upserted.", n)
    return n


def run_seismic_flow(
    days_back: int = 7,
    target: str = "database",
) -> None:
    """Dagster flow: daily KNMI seismic event ingestion."""
    fetch_seismic_events(days_back=days_back, target=target)


def run_seismic_repair_flow(
    start_date: str,
    end_date: str | None = None,
    target: str = "database",
    min_magnitude: float = 0.0,
) -> None:
    """Dagster flow: repair KNMI seismic events for a date range."""
    start = datetime_date.fromisoformat(start_date)
    end = datetime_date.fromisoformat(end_date) if end_date else start
    fetch_seismic_events_for_range(
        start_date=start,
        end_date=end,
        target=target,
        min_magnitude=min_magnitude,
    )
