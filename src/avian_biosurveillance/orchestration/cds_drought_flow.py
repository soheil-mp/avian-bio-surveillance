"""Dagster flows for Copernicus CDS drought ingestion."""

from __future__ import annotations

import logging
from datetime import date

from avian_biosurveillance.ingestion.cds_drought import (
    CDSDroughtCollector,
    parse_month,
)


logger = logging.getLogger(__name__)


def _cds_drought_run_name() -> str:
    """Generate a stable daily flow-run name."""
    return f"cds-drought-ingest/{date.today().isoformat()}"


def _raise_for_cds_failures(results: list[object]) -> None:
    """Fail orchestration when any CDS month had hard request failures."""
    failed_months = [
        getattr(item, "month", "unknown")
        for item in results
        if getattr(item, "requests_failed", 0)
    ]
    empty_months = [
        getattr(item, "month", "unknown")
        for item in results
        if getattr(item, "requests_attempted", 0)
        and not getattr(item, "requests_downloaded", 0)
        and not getattr(item, "rows_parsed", 0)
    ]
    if failed_months or empty_months:
        parts: list[str] = []
        if failed_months:
            parts.append(f"failed requests: {', '.join(failed_months)}")
        if empty_months:
            parts.append(f"no data downloaded: {', '.join(empty_months)}")
        msg = "CDS drought catchup incomplete (" + "; ".join(parts) + ")"
        raise RuntimeError(msg)


def fetch_cds_drought_profiles(
    target: str = "database",
    lookback_months: int = 4,
    include_ensemble_members: bool = True,
) -> None:
    """Ingest the last *lookback_months* complete CDS drought months."""
    logger.info(
        "Starting CDS drought ingestion for the last %d complete month(s).",
        lookback_months,
    )
    collector = CDSDroughtCollector(
        include_ensemble_members=include_ensemble_members,
    )
    results = collector.collect_recent_months(
        lookback_months,
        target=target,
    )
    _raise_for_cds_failures(results)
    logger.info(
        "CDS drought ingestion complete — %d month(s), %d parsed rows, %d upserted rows.",
        len(results),
        sum(item.rows_parsed for item in results),
        sum(item.rows_upserted for item in results),
    )


def run_cds_drought_flow(
    target: str = "database",
    lookback_months: int = 4,
    include_ensemble_members: bool = True,
) -> None:
    """Dagster flow: daily CDS drought ingestion."""
    fetch_cds_drought_profiles(
        target=target,
        lookback_months=lookback_months,
        include_ensemble_members=include_ensemble_members,
    )


def _cds_drought_catchup_run_name(
    start_month: str = "unknown",
    end_month: str | None = None,
    **_kwargs: object,
) -> str:
    """Generate a stable catchup flow-run name."""
    end = end_month or start_month
    return f"cds-drought-catchup/{start_month}..{end}"


def _previous_month_start(reference_day: date) -> date:
    """Return the first day of the previous calendar month."""
    this_month = reference_day.replace(day=1)
    if this_month.month == 1:
        return date(this_month.year - 1, 12, 1)
    return date(this_month.year, this_month.month - 1, 1)


def backfill_cds_drought_profiles(
    start_month: date,
    end_month: date,
    force: bool = False,
    target: str = "database",
    include_ensemble_members: bool = True,
) -> None:
    """Catch up a CDS drought month range."""
    logger.info(
        "CDS drought catchup: %s to %s (force=%s).",
        start_month,
        end_month,
        force,
    )
    collector = CDSDroughtCollector(
        include_ensemble_members=include_ensemble_members,
    )
    results = collector.collect_month_range(
        start_month,
        end_month,
        target=target,
        force=force,
    )
    _raise_for_cds_failures(results)
    logger.info(
        "CDS drought catchup complete — %d month(s), %d parsed rows, %d upserted rows.",
        len(results),
        sum(item.rows_parsed for item in results),
        sum(item.rows_upserted for item in results),
    )


def run_cds_drought_catchup_flow(
    start_month: str,
    end_month: str | None = None,
    force: bool = False,
    target: str = "database",
    include_ensemble_members: bool = True,
) -> None:
    """Dagster flow: CDS drought month-range catchup."""
    start = parse_month(start_month)
    end = parse_month(end_month) if end_month else _previous_month_start(date.today())

    if end < start:
        msg = f"end_month ({end}) must be >= start_month ({start})"
        raise ValueError(msg)

    backfill_cds_drought_profiles(
        start_month=start,
        end_month=end,
        force=force,
        target=target,
        include_ensemble_members=include_ensemble_members,
    )
