"""Earth Engine environmental profiling flows.

Dagster tasks and flows for satellite-derived environmental profiling
from `Google Earth Engine <https://earthengine.google.com/>`_.

``run_earth_engine_flow`` *(bi-weekly, legacy alias)*
    Compatibility alias for the canonical Netherlands base-grid flow.

``run_earth_engine_grid_flow`` *(bi-weekly)*
    Profile the bi-weekly (1st/15th) anchors within *lookback_days* for the
    canonical Netherlands base grid.

``run_earth_engine_repair_flow`` *(manual, legacy alias)*
    Compatibility alias for the canonical Netherlands base-grid repair flow.

``run_earth_engine_grid_repair_flow`` *(manual)*
    Repair an explicit date range for the canonical Netherlands base grid.
"""

from __future__ import annotations

import logging
import os
from datetime import date, timedelta

from avian_biosurveillance.ingestion.earth_engine import (
    DEFAULT_EE_GRID_CELL_WORKERS,
    DEFAULT_EE_GRID_DB_BATCH_SIZE,
    DEFAULT_EE_GRID_SIZE_KM,
    DEFAULT_EE_SECTION_WORKERS,
    EarthEngineGridCollector,
)
from avian_biosurveillance.ingestion.earth_engine_cadence import (
    anchor_dates_in_range,
)


logger = logging.getLogger(__name__)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using default %d.", name, raw, default)
        return default


DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS = DEFAULT_EE_SECTION_WORKERS
DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS = DEFAULT_EE_GRID_CELL_WORKERS
DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE = DEFAULT_EE_GRID_DB_BATCH_SIZE
DEFAULT_EARTH_ENGINE_HIGH_VOLUME = _env_bool("EARTH_ENGINE_HIGH_VOLUME", True)
DEFAULT_EARTH_ENGINE_GRID_SIZE_KM = DEFAULT_EE_GRID_SIZE_KM

# Earth Engine is ingested on a bi-weekly cadence (1st/15th anchors).  The
# scheduled lookback window is wide enough to (re)cover the most recent matured
# anchor given upstream publication latency (MODIS ~3 weeks, ERA5-Land ~6 days);
# already-complete anchor days are skipped cheaply by the collector.
DEFAULT_EARTH_ENGINE_LOOKBACK_DAYS = _env_int(
    "EARTH_ENGINE_LOOKBACK_DAYS", 30, minimum=1
)


def _earth_engine_run_name() -> str:
    """Generate run name: ``earth-engine-ingest/YYYY-MM-DD``."""
    return f"earth-engine-ingest/{date.today().isoformat()}"


def _earth_engine_grid_run_name() -> str:
    """Generate run name: ``earth-engine-grid-ingest/YYYY-MM-DD``."""
    return f"earth-engine-grid-ingest/{date.today().isoformat()}"


def _collect_grid_profiles(
    *,
    target: str,
    start_date: date,
    end_date: date,
    grid_size_km: float,
    max_workers: int,
    cell_workers: int,
    db_batch_size: int,
    high_volume: bool,
    force: bool = False,
    only_dates: list[date] | None = None,
) -> None:
    collector = EarthEngineGridCollector(
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
    )
    collect_kwargs: dict[str, object] = {
        "start_date": start_date,
        "end_date": end_date,
        "target": target,
    }
    if force:
        collect_kwargs["force"] = True
    if only_dates is not None:
        collect_kwargs["only_dates"] = only_dates

    results = collector.collect_date_range(
        **collect_kwargs,
    )
    if not results:
        msg = (
            "Earth Engine grid profiling produced no per-day results; "
            "check grid discovery and source availability."
        )
        raise RuntimeError(msg)

    total = sum(s.locations_profiled for s in results)
    skipped = sum(getattr(s, "locations_skipped", 0) for s in results)
    failed = sum(s.locations_failed for s in results)
    logger.info(
        "Earth Engine grid profiling complete at %.1f km — "
        "%d grid profiles, %d skipped, %d failures.",
        grid_size_km,
        total,
        skipped,
        failed,
    )
    if failed:
        msg = f"Earth Engine grid profiling failed for {failed} location(s)."
        raise RuntimeError(msg)


def _collect_grid_anchor_profiles(
    *,
    target: str,
    lookback_days: int,
    grid_size_km: float,
    max_workers: int,
    cell_workers: int,
    db_batch_size: int,
    high_volume: bool,
) -> None:
    """Profile the Netherlands base grid for bi-weekly anchors in the lookback window.

    Only the 1st/15th anchor dates inside ``[today - lookback_days + 1, today]``
    are ingested; already-complete anchor days are skipped by the collector.
    """
    today = date.today()
    start = today - timedelta(days=lookback_days - 1)
    anchors = anchor_dates_in_range(start, today)
    if not anchors:
        logger.info(
            "No Earth Engine bi-weekly anchor date due in %s to %s; skipping.",
            start,
            today,
        )
        return
    logger.info(
        "Starting Earth Engine grid profiling for %d bi-weekly anchor(s) "
        "(%s to %s) at %.1f km.",
        len(anchors),
        anchors[0],
        anchors[-1],
        grid_size_km,
    )
    _collect_grid_profiles(
        target=target,
        start_date=anchors[0],
        end_date=anchors[-1],
        only_dates=anchors,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
    )


def fetch_earth_engine_profiles(
    target: str = "database",
    lookback_days: int = DEFAULT_EARTH_ENGINE_LOOKBACK_DAYS,
    grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
) -> None:
    """Legacy alias for the canonical bi-weekly Netherlands base-grid collector."""
    _collect_grid_anchor_profiles(
        target=target,
        lookback_days=lookback_days,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
    )


def fetch_earth_engine_grid_profiles(
    target: str = "database",
    lookback_days: int = DEFAULT_EARTH_ENGINE_LOOKBACK_DAYS,
    grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
) -> None:
    """Profile the Netherlands base grid for bi-weekly anchors in the lookback window."""
    _collect_grid_anchor_profiles(
        target=target,
        lookback_days=lookback_days,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
    )


def run_earth_engine_flow(
    target: str = "database",
    lookback_days: int = DEFAULT_EARTH_ENGINE_LOOKBACK_DAYS,
    grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
) -> None:
    """Legacy alias for the canonical bi-weekly Earth Engine base-grid flow."""
    fetch_earth_engine_profiles(
        target=target,
        lookback_days=lookback_days,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
    )


def run_earth_engine_grid_flow(
    target: str = "database",
    lookback_days: int = DEFAULT_EARTH_ENGINE_LOOKBACK_DAYS,
    grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
) -> None:
    """Dagster flow: bi-weekly Earth Engine base-grid profiling."""
    fetch_earth_engine_grid_profiles(
        target=target,
        lookback_days=lookback_days,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
    )


# ==========================================================================
# Historic date-range backfill flow
# ==========================================================================


def _earth_engine_repair_run_name(
    start_date: str = "unknown",
    end_date: str | None = None,
    **_kwargs: object,
) -> str:
    """Generate run name: ``earth-engine-repair/YYYY-MM-DD..YYYY-MM-DD``."""
    end = end_date or start_date
    return f"earth-engine-repair/{start_date}..{end}"


def _earth_engine_grid_repair_run_name(
    start_date: str = "unknown",
    end_date: str | None = None,
    **_kwargs: object,
) -> str:
    """Generate run name: ``earth-engine-grid-repair/YYYY-MM-DD..YYYY-MM-DD``."""
    end = end_date or start_date
    return f"earth-engine-grid-repair/{start_date}..{end}"


def backfill_earth_engine_grid_day(
    obs_date: date,
    force: bool = False,
    target: str = "database",
    grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
) -> None:
    """Repair Earth Engine base-grid profiles for one calendar day."""
    logger.info(
        "Earth Engine grid repair day: %s (force=%s, grid_size_km=%.1f).",
        obs_date,
        force,
        grid_size_km,
    )
    _collect_grid_profiles(
        target=target,
        start_date=obs_date,
        end_date=obs_date,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
        force=force,
    )


def backfill_earth_engine_profiles(
    start_date: date,
    end_date: date,
    force: bool = False,
    target: str = "database",
    grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
) -> None:
    """Legacy alias for base-grid Earth Engine repair."""
    logger.info(
        "Station-based Earth Engine repair is retired; using the canonical "
        "Netherlands %.1f km base grid for %s to %s (force=%s).",
        grid_size_km,
        start_date,
        end_date,
        force,
    )
    _collect_grid_profiles(
        target=target,
        start_date=start_date,
        end_date=end_date,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
        force=force,
    )


def backfill_earth_engine_grid_profiles(
    start_date: date,
    end_date: date,
    force: bool = False,
    target: str = "database",
    grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
) -> None:
    """Repair Earth Engine base-grid profiles for an explicit date range."""
    logger.info(
        "Earth Engine grid repair: %s to %s (force=%s, grid_size_km=%.1f).",
        start_date,
        end_date,
        force,
        grid_size_km,
    )
    _collect_grid_profiles(
        target=target,
        start_date=start_date,
        end_date=end_date,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
        force=force,
    )


def run_earth_engine_repair_flow(
    start_date: str,
    end_date: str | None = None,
    force: bool = False,
    target: str = "database",
    grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
) -> None:
    """Legacy alias for Earth Engine base-grid profiling repair.

    Args:
        start_date: First date to profile (``YYYY-MM-DD``).
        end_date: Last date to profile (inclusive).  Defaults to today.
        force: Re-profile days that already have output on disk.
        target: ``"local"`` (disk only) or ``"database"`` (disk + DB).
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date) if end_date else date.today()

    if end < start:
        msg = f"end_date ({end}) must be >= start_date ({start})"
        raise ValueError(msg)

    backfill_earth_engine_profiles(
        start_date=start,
        end_date=end,
        force=force,
        target=target,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
    )


def run_earth_engine_grid_repair_flow(
    start_date: str,
    end_date: str | None = None,
    force: bool = False,
    target: str = "database",
    grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
) -> None:
    """Dagster flow: Earth Engine grid profiling repair."""
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date) if end_date else date.today()

    if end < start:
        msg = f"end_date ({end}) must be >= start_date ({start})"
        raise ValueError(msg)

    backfill_earth_engine_grid_profiles(
        start_date=start,
        end_date=end,
        force=force,
        target=target,
        grid_size_km=grid_size_km,
        max_workers=max_workers,
        cell_workers=cell_workers,
        db_batch_size=db_batch_size,
        high_volume=high_volume,
    )
