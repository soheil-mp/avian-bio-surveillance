"""eBird observation ingestion flow.

Dagster tasks and flows for ingesting bird observations from the
`eBird <https://ebird.org>`_ API.

``run_ebird_flow`` *(daily)*
    Fetch the last *back* days of recent observations.

``run_ebird_repair_flow`` *(manual)*
    Fetch historic observations for an explicit date range,
    skipping dates already ingested unless ``force=True``.
"""

from __future__ import annotations

import logging
from datetime import date, timedelta

from avian_biosurveillance.ingestion.ebird import EBirdCollector


logger = logging.getLogger(__name__)


def _ebird_run_name() -> str:
    """Generate run name: ``ebird-ingest/YYYY-MM-DD``."""
    return f"ebird-ingest/{date.today().isoformat()}"


def fetch_ebird_data(
    region: str = "NL",
    back: int = 3,
    target_species_only: bool = True,
    target: str = "database",
) -> None:
    """Fetch recent eBird observations for a given region.

    Delegates to :class:`~avian_biosurveillance.ingestion.ebird.EBirdCollector`.

    Args:
        region: eBird region code (default ``NL`` = Netherlands).
        back: Days back to fetch (1-30, per eBird API limits).
        target_species_only: When *True*, only fetch species present in
            ``config/target_species.json``.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
    """
    logger.info("Starting eBird ingestion for region=%s, back=%d days.", region, back)
    collector = EBirdCollector()
    stats = collector.collect_recent(
        region=region,
        back=back,
        target_species_only=target_species_only,
        target=target,
    )
    logger.info("eBird ingestion complete — %s", stats)


def run_ebird_flow(
    region: str = "NL",
    back: int = 3,
    target_species_only: bool = True,
    target: str = "database",
) -> None:
    """Dagster flow: daily eBird observation ingestion."""
    fetch_ebird_data(
        region=region,
        back=back,
        target_species_only=target_species_only,
        target=target,
    )


# ==========================================================================
# Historic date-range backfill flow
# ==========================================================================


def backfill_ebird_day(
    obs_date: date,
    region: str = "NL",
    target_species_only: bool = True,
    target: str = "database",
) -> None:
    """Fetch historic eBird observations for a single calendar day.

    Uses :meth:`~avian_biosurveillance.ingestion.ebird.EBirdCollector.collect_historic`
    which skips dates already present on disk by default.

    Args:
        obs_date: The calendar date to ingest.
        region: eBird region code.
        target_species_only: Restrict to target species.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
    """
    collector = EBirdCollector()
    stats = collector.collect_historic(
        region=region,
        obs_date=obs_date,
        target_species_only=target_species_only,
        target=target,
    )
    if stats.api_errors:
        msg = f"[{obs_date}] eBird repair encountered {stats.api_errors} API error(s)."
        raise RuntimeError(msg)
    logger.info("[%s] eBird repair — %s", obs_date, stats)


def _ebird_repair_run_name(
    start_date: str = "unknown",
    end_date: str | None = None,
    **_kwargs: object,
) -> str:
    """Generate run name: ``ebird-repair/YYYY-MM-DD..YYYY-MM-DD``."""
    end = end_date or start_date
    return f"ebird-repair/{start_date}..{end}"


def run_ebird_repair_flow(
    start_date: str,
    end_date: str | None = None,
    region: str = "NL",
    target_species_only: bool = True,
    target: str = "database",
) -> dict:
    """Dagster flow: repair eBird observations for a date range.

    Submits one task per day so Dagster tracks per-day success/failure
    independently.

    Args:
        start_date: First date to ingest (``YYYY-MM-DD``).
        end_date: Last date to ingest (inclusive).  Defaults to
            *start_date* (single day).
        region: eBird region code.
        target_species_only: Restrict to target species.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).

    Returns:
        Summary dict with ``days_total``, ``days_completed``, and
        ``days_failed``.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date) if end_date else start

    if end < start:
        msg = f"end_date ({end}) must be >= start_date ({start})"
        raise ValueError(msg)

    total_days = (end - start).days + 1
    logger.info(
        "eBird repair: %d day(s) from %s to %s (region=%s).",
        total_days,
        start,
        end,
        region,
    )

    # Run one day at a time, newest first, so the most recent data lands first.
    per_day: list[dict[str, object]] = []
    completed = failed = 0
    current = end
    while current >= start:
        date_str = current.isoformat()
        try:
            backfill_ebird_day(
                obs_date=current,
                region=region,
                target_species_only=target_species_only,
                target=target,
            )
            per_day.append({"date": date_str, "failed": False})
            completed += 1
        except Exception as exc:
            logger.error("[%s] eBird repair failed: %s", date_str, exc)
            per_day.append({"date": date_str, "failed": True, "error": str(exc)})
            failed += 1
        current -= timedelta(days=1)

    summary = {
        "start_date": start_date,
        "end_date": end_date or start_date,
        "days_total": total_days,
        "days_completed": completed,
        "days_failed": failed,
        "per_day": per_day,
    }
    logger.info(
        "eBird repair done: %d completed, %d failed.",
        completed,
        failed,
    )
    if failed:
        msg = f"eBird repair failed for {failed} day(s)."
        raise RuntimeError(msg)
    return summary
