"""Legacy daily pipeline compatibility wrapper.

Manual entry point retained for ad hoc CLI and notebook runs. The canonical
runtime orchestration now lives in the Dagster schedule, sensor, and asset
graph definitions under :mod:`avian_biosurveillance.orchestration.dagster_defs`.

This module remains available for direct Python callers, but it is not the
primary orchestration surface and is intentionally not registered as a Dagster
job.

Execution order:

Phase 1 — **Ingest** (mixed)
    Non-Earth-Engine sources are submitted via lightweight thread-backed
    futures so the slow audio download in BirdWeather does not serialize
    KNMI / eBird. Earth Engine grid profiling and Sovon
    extraction then run sequentially to avoid GEE/CPU contention.

Phase 2 — **Post-process** (sequential, ``target=='database'`` only)
    BirdWeather deduplication, station-summary refresh, species-tracker.

Phase 3 — **Audit**
    ISO 25012 quality audit across all source tables.

Phase 4 — **Audio processing**
    Crop BirdWeather raw soundscapes into processed clips.

Phase 5 — **Feature engineering**
    Generate BirdWeather audio artifacts, embeddings, and background tags.

This module is **not** registered as a Dagster job (each source has its own
schedule and asset job). Prefer the Dagster orchestration layer for routine
execution.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from typing import Any

from avian_biosurveillance.orchestration import (
    birdweather_flow,
    data_quality_flow,
    derived_aggregates_flow,
    earth_engine_flow,
    ebird_flow,
    knmi_flow,
    sovon_flow,
    station_summary_flow,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tiny thread-pool task adapter.
# Tests monkeypatch the module-level ``fetch_*`` names with their own
# ``submit()``-capable stubs, so this adapter is only used in real runs.
# ---------------------------------------------------------------------------


class _Task:
    """Wrap a callable so it can be invoked directly *or* via ``.submit()``."""

    def __init__(self, fn: Callable[..., Any], *, executor: ThreadPoolExecutor):
        self._fn = fn
        self._executor = executor

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self._fn(*args, **kwargs)

    def submit(self, **kwargs: Any):
        return self._executor.submit(self._fn, **kwargs)


_executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="daily-pipeline")

fetch_birdweather_data = _Task(
    birdweather_flow.fetch_birdweather_data, executor=_executor
)
fetch_knmi_data = _Task(knmi_flow.fetch_knmi_data, executor=_executor)
fetch_seismic_events = _Task(knmi_flow.fetch_seismic_events, executor=_executor)
fetch_ebird_data = _Task(ebird_flow.fetch_ebird_data, executor=_executor)
download_sovon_charts = _Task(sovon_flow.download_sovon_charts, executor=_executor)

# Functions called directly (not via ``.submit``) — kept here so tests can
# monkeypatch ``daily_pipeline_flow.<name>`` instead of reaching into the
# original modules.
fetch_earth_engine_grid_profiles = earth_engine_flow.fetch_earth_engine_grid_profiles
extract_sovon_data = sovon_flow.extract_sovon_data
deduplicate_birdweather_detections = birdweather_flow.deduplicate_birdweather_detections
refresh_station_summary_task = station_summary_flow.refresh_station_summary_task
refresh_species_tracker_task = derived_aggregates_flow.refresh_species_tracker_task
run_quality_audit_task = data_quality_flow.run_quality_audit_task


def process_birdweather_audio_clips(*args: Any, **kwargs: Any) -> Any:
    """Lazy proxy to :func:`processing.birdweather_audio.process_birdweather_audio_clips`."""
    from avian_biosurveillance.processing.birdweather_audio import (
        process_birdweather_audio_clips as _impl,
    )

    return _impl(*args, **kwargs)


def engineer_birdweather_audio_features(*args: Any, **kwargs: Any) -> Any:
    """Lazy proxy to :func:`features.birdweather_features.engineer_birdweather_audio_features`."""
    from avian_biosurveillance.features.birdweather_features import (
        engineer_birdweather_audio_features as _impl,
    )

    return _impl(*args, **kwargs)


def _daily_run_name() -> str:
    """Return a human-readable run name: ``daily-pipeline/YYYY-MM-DD``."""
    return f"daily-pipeline/{date.today().isoformat()}"


def run_daily_pipeline(
    # BirdWeather
    bw_hours: int = 24,
    bw_country: str = "Netherlands",
    bw_species_ids: list[str] | None = None,
    bw_min_confidence: float = 0.1,
    bw_workers: int = 20,
    # KNMI
    knmi_hours_back: int = 26,
    # Seismic
    seismic_days_back: int = 7,
    # eBird
    ebird_region: str = "NL",
    ebird_back: int = 3,
    # Earth Engine
    ee_lookback_days: int = 3,
    # General
    target: str = "database",
) -> None:
    """Daily pipeline: parallel ingestion → post-processing → quality audit."""
    from avian_biosurveillance.config.species import registry as _sp_registry
    from avian_biosurveillance.utils.log_utils import fmt

    species_ids = bw_species_ids or list(_sp_registry.birdweather_ids)

    # ── Phase 1: Parallel ingestion ─────────────────────────────────
    logger.info(fmt.phase("PHASE 1: PARALLEL INGESTION"))

    futures = {
        "birdweather": fetch_birdweather_data.submit(
            hours=bw_hours,
            country=bw_country,
            species_ids=species_ids,
            min_confidence=bw_min_confidence,
            workers=bw_workers,
            target=target,
        ),
        "knmi": fetch_knmi_data.submit(target=target, hours_back=knmi_hours_back),
        "seismic": fetch_seismic_events.submit(
            days_back=seismic_days_back, target=target
        ),
        "ebird": fetch_ebird_data.submit(
            region=ebird_region,
            back=ebird_back,
            target_species_only=True,
            target=target,
        ),
        "sovon_download": download_sovon_charts.submit(target=target),
    }

    failed: list[str] = []
    sovon_stage_root = None
    for source, future in futures.items():
        try:
            result = future.result()
            if source == "sovon_download":
                sovon_stage_root = result
            logger.info(fmt.done(source))
        except Exception:  # pragma: no cover - logged for visibility
            logger.error(fmt.fail(source), exc_info=True)
            failed.append(source)

    if failed:
        logger.warning("Phase 1 finished with %d failure(s): %s", len(failed), failed)
    else:
        logger.info(fmt.phase("PHASE 1 COMPLETE"))

    # Earth Engine after the parallel batch to avoid quota overlap.
    try:
        fetch_earth_engine_grid_profiles(
            target=target,
            lookback_days=ee_lookback_days,
            grid_size_km=5.0,
        )
        logger.info(fmt.done("earth_engine_grid"))
    except Exception:
        logger.error(fmt.fail("earth_engine_grid"), exc_info=True)
        failed.append("earth_engine_grid")

    if "sovon_download" not in failed:
        try:
            extract_sovon_data(target=target, stage_root=sovon_stage_root)
            logger.info(fmt.done("sovon_extraction"))
        except Exception:
            logger.error(fmt.fail("sovon_extraction"), exc_info=True)

    if target != "database":
        logger.info(
            "Phases 2-5 skipped (target=%s).",
            target,
        )
        logger.info(fmt.phase("DAILY PIPELINE COMPLETE"))
        return

    # ── Phase 2: Post-processing ────────────────────────────────────
    logger.info(fmt.phase("PHASE 2: POST-PROCESSING"))
    deduplicate_birdweather_detections()
    refresh_station_summary_task()
    refresh_species_tracker_task()

    # ── Phase 3: Quality audit ──────────────────────────────────────
    logger.info(fmt.phase("PHASE 3: QUALITY AUDIT"))
    run_quality_audit_task()

    # ── Phase 4: Audio processing ───────────────────────────────────
    logger.info(fmt.phase("PHASE 4: AUDIO PROCESSING"))
    process_birdweather_audio_clips(storage_retry_attempts=2)

    # ── Phase 5: Feature engineering ────────────────────────
    logger.info(fmt.phase("PHASE 5: FEATURE ENGINEERING"))
    engineer_birdweather_audio_features()

    logger.info(fmt.phase("DAILY PIPELINE COMPLETE"))
