"""Historical catchup pipeline.

Runs the date-granular ingestion tasks source-by-source for each calendar day
in chronological order, then executes the same database follow-up chain used by
the live daily pipeline. This keeps BirdWeather station summaries, the quality
audit, processed clips, and audio features aligned with the actual catchup day
that just landed.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from functools import partial

from avian_biosurveillance.config.species import registry as _sp_registry
from avian_biosurveillance.features.birdweather_features import (
    engineer_birdweather_audio_features,
)
from avian_biosurveillance.orchestration.birdweather_flow import (
    _raise_for_audio_processing_followup_failures,
    _raise_for_feature_engineering_followup_failures,
    _raise_for_stat_failures,
    backfill_birdweather_day,
    deduplicate_birdweather_detections,
)
from avian_biosurveillance.orchestration.data_quality_flow import (
    run_quality_audit_task,
)
from avian_biosurveillance.orchestration.derived_aggregates_flow import (
    refresh_species_dim_task,
    refresh_species_tracker_task,
)
from avian_biosurveillance.orchestration.earth_engine_flow import (
    DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
    backfill_earth_engine_grid_day,
)
from avian_biosurveillance.orchestration.ebird_flow import backfill_ebird_day
from avian_biosurveillance.orchestration.knmi_flow import backfill_knmi_day
from avian_biosurveillance.orchestration.station_summary_flow import (
    refresh_station_summary_task,
)
from avian_biosurveillance.processing.birdweather_audio import (
    process_birdweather_audio_clips,
)
from avian_biosurveillance.processing.birdweather_raw_audio import (
    download_birdweather_raw_audio_objects,
)
from avian_biosurveillance.utils.log_utils import fmt


logger = logging.getLogger(__name__)

_HISTORICAL_CATCHUP_FOLLOWUP_BATCH_DAYS = 7
_SHARED_HISTORICAL_SOURCES = frozenset({"knmi", "ebird", "earth_engine_grid"})
_SUPPORTED_HISTORICAL_SOURCES = _SHARED_HISTORICAL_SOURCES | {"birdweather"}


def _historical_catchup_run_name(
    start_date: str = "unknown",
    end_date: str | None = None,
    **_kwargs: object,
) -> str:
    end = end_date or start_date
    return f"historical-catchup/{start_date}..{end}"


def _iter_days_asc(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _active_sources(sources: str | list[str] | None) -> set[str]:
    if sources is None:
        return set(_SHARED_HISTORICAL_SOURCES)
    values = sources.split(",") if isinstance(sources, str) else sources
    active_sources = {str(value).strip() for value in values if str(value).strip()}
    unsupported = active_sources - _SUPPORTED_HISTORICAL_SOURCES
    if unsupported:
        msg = (
            f"Unsupported historical catchup sources: {', '.join(sorted(unsupported))}"
        )
        raise ValueError(msg)
    return active_sources


def _run_historical_catchup_followup(
    start_date: date,
    end_date: date,
    *,
    active_sources: set[str],
    bw_download_audio: bool,
    bw_workers: int,
    run_tracker_refresh: bool,
) -> None:
    """Run shared historical follow-up work for an inclusive date window."""
    logger.info(
        fmt.phase(f"FOLLOW-UP BATCH {start_date.isoformat()}..{end_date.isoformat()}")
    )
    if "birdweather" in active_sources:
        deduplicate_birdweather_detections()
        refresh_station_summary_task()
    if {"birdweather", "ebird"} & active_sources:
        refresh_species_dim_task()
    if "birdweather" in active_sources:
        if bw_download_audio:
            raw_audio_stats = download_birdweather_raw_audio_objects(
                lookback_days=None,
                limit=None,
                start_date=start_date,
                end_date=end_date,
                workers=bw_workers,
            )
            _raise_for_stat_failures(
                "BirdWeather raw audio",
                raw_audio_stats,
                ("failed", "upload_failed"),
            )
        current = start_date
        while current <= end_date:
            audio_stats = process_birdweather_audio_clips(
                lookback_days=None,
                limit=None,
                start_date=current,
                end_date=current,
                storage_retry_attempts=2,
            )
            _raise_for_audio_processing_followup_failures(
                f"BirdWeather audio processing {current.isoformat()}",
                audio_stats,
            )
            feature_stats = engineer_birdweather_audio_features(
                lookback_days=None,
                limit=None,
                start_date=current,
                end_date=current,
            )
            _raise_for_feature_engineering_followup_failures(
                f"BirdWeather feature engineering {current.isoformat()}",
                feature_stats,
            )
            current += timedelta(days=1)
    run_quality_audit_task()
    if run_tracker_refresh:
        refresh_species_tracker_task(start_date=start_date, end_date=end_date)


def run_historical_catchup_flow(
    start_date: str,
    end_date: str | None = None,
    # BirdWeather
    bw_country: str = "Netherlands",
    bw_species_ids: list[str] | None = None,
    bw_all_species: bool = False,
    bw_min_confidence: float = 0.1,
    bw_workers: int = 20,
    bw_download_audio: bool = False,
    # KNMI
    knmi_force: bool = False,
    # eBird
    ebird_region: str = "NL",
    ebird_target_species_only: bool = True,
    # Earth Engine
    ee_grid_size_km: float = DEFAULT_EARTH_ENGINE_GRID_SIZE_KM,
    ee_force: bool = False,
    ee_max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    ee_cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    ee_db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    ee_high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
    # General
    target: str = "database",
    sources: str | list[str] | None = None,
    followup_batch_days: int = _HISTORICAL_CATCHUP_FOLLOWUP_BATCH_DAYS,
    run_tracker_refresh: bool = True,
) -> dict[str, object]:
    """Catch up the main date-based sources one day at a time.

    Each calendar day is ingested in chronological order. Database-target
    follow-up work is batched over the completed catchup window so expensive
    full-table refreshes do not run once per historical day.
    The flow intentionally excludes non date-granular sources such as Sovon,
    CDS drought, and the rolling-window seismic collector.
    """
    start = date.fromisoformat(start_date)
    end = date.fromisoformat(end_date) if end_date else start
    if end < start:
        msg = f"end_date ({end}) must be >= start_date ({start})"
        raise ValueError(msg)
    if followup_batch_days < 1:
        msg = "followup_batch_days must be >= 1"
        raise ValueError(msg)

    if bw_all_species and bw_species_ids:
        msg = "bw_species_ids cannot be provided when bw_all_species=True"
        raise ValueError(msg)
    species_ids = (
        None
        if bw_all_species
        else bw_species_ids or list(_sp_registry.birdweather_ids)
    )
    active_sources = _active_sources(sources)
    per_day: list[dict[str, object]] = []
    days_completed = 0
    days_with_failures = 0
    batch_start = start

    for current_day in _iter_days_asc(start, end):
        logger.info(fmt.phase(f"HISTORICAL CATCHUP DAY {current_day.isoformat()}"))
        failures: list[str] = []

        source_runners: tuple[tuple[str, Callable[[], object]], ...] = (
            (
                "birdweather",
                partial(
                    backfill_birdweather_day,
                    obs_date=current_day,
                    country=bw_country,
                    species_ids=species_ids,
                    all_species=bw_all_species,
                    min_confidence=bw_min_confidence,
                    workers=bw_workers,
                    download_audio=bw_download_audio,
                    target=target,
                ),
            ),
            (
                "knmi",
                partial(
                    backfill_knmi_day,
                    date=current_day,
                    target=target,
                    force=knmi_force,
                ),
            ),
            (
                "ebird",
                partial(
                    backfill_ebird_day,
                    obs_date=current_day,
                    region=ebird_region,
                    target_species_only=ebird_target_species_only,
                    target=target,
                ),
            ),
            (
                "earth_engine_grid",
                partial(
                    backfill_earth_engine_grid_day,
                    obs_date=current_day,
                    force=ee_force,
                    target=target,
                    grid_size_km=ee_grid_size_km,
                    max_workers=ee_max_workers,
                    cell_workers=ee_cell_workers,
                    db_batch_size=ee_db_batch_size,
                    high_volume=ee_high_volume,
                ),
            ),
        )

        for source, runner in source_runners:
            if source not in active_sources:
                logger.info(fmt.skip(f"{source}:{current_day.isoformat()}"))
                continue
            try:
                runner()
                logger.info(fmt.done(f"{source}:{current_day.isoformat()}"))
            except Exception:  # pylint: disable=broad-exception-caught
                logger.error(
                    fmt.fail(f"{source}:{current_day.isoformat()}"),
                    exc_info=True,
                )
                failures.append(source)

        days_completed += 1
        if failures:
            days_with_failures += 1
        per_day.append(
            {
                "date": current_day.isoformat(),
                "failed_sources": failures,
                "sources": sorted(active_sources),
                "target": target,
            }
        )

        if failures:
            msg = (
                f"Historical catchup failed for {current_day.isoformat()}: "
                f"{', '.join(failures)}"
            )
            raise RuntimeError(msg)

        if target == "database" and (
            (current_day - batch_start).days + 1 >= followup_batch_days
            or current_day == end
        ):
            _run_historical_catchup_followup(
                batch_start,
                current_day,
                active_sources=active_sources,
                bw_download_audio=bw_download_audio,
                bw_workers=bw_workers,
                run_tracker_refresh=run_tracker_refresh,
            )
            batch_start = current_day + timedelta(days=1)

    return {
        "start_date": start_date,
        "end_date": end_date or start_date,
        "days_total": (end - start).days + 1,
        "days_completed": days_completed,
        "days_with_failures": days_with_failures,
        "per_day": per_day,
    }
