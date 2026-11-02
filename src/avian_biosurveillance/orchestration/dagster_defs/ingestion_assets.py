"""Dagster assets for ingestion logic.

Each ingestion source is exposed as a Software-Defined Asset. The matching
flow key in ``config/schedules.json`` drives both the daily schedule and the
sensor-driven follow-up chain (quality audit, station-summary refresh, audio
processing, feature engineering).
"""

from dagster import (
    AssetExecutionContext,
    Backoff,
    Config,
    MaterializeResult,
    RetryPolicy,
    asset,
)

from avian_biosurveillance.orchestration.birdweather_flow import (
    deduplicate_birdweather_detections,
    fetch_birdweather_data,
)
from avian_biosurveillance.orchestration.cds_drought_flow import (
    fetch_cds_drought_profiles,
)
from avian_biosurveillance.orchestration.earth_engine_flow import (
    DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
    DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
    DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
    DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
    DEFAULT_EARTH_ENGINE_LOOKBACK_DAYS,
    fetch_earth_engine_grid_profiles,
    fetch_earth_engine_profiles,
)
from avian_biosurveillance.orchestration.ebird_flow import fetch_ebird_data
from avian_biosurveillance.orchestration.knmi_flow import (
    fetch_knmi_data,
    fetch_seismic_events,
)
from avian_biosurveillance.orchestration.movebank_flow import fetch_movebank_data
from avian_biosurveillance.orchestration.sovon_flow import run_sovon_flow


# ---------------------------------------------------------------------------
# Optional pydantic configuration structs for the ingestion assets.
# ---------------------------------------------------------------------------


class BirdweatherConfig(Config):
    hours: int = 24
    country: str = "Netherlands"
    species_ids: list[str] | None = None
    all_species: bool = False
    min_confidence: float = 0.1
    workers: int = 20
    target: str = "local"
    download_audio: bool = True


class EbirdConfig(Config):
    region: str = "NL"
    back: int = 3
    target_species_only: bool = True
    target: str = "database"


class MovebankConfig(Config):
    lookback_days: int = 7
    target: str = "database"


class EarthEngineConfig(Config):
    lookback_days: int = DEFAULT_EARTH_ENGINE_LOOKBACK_DAYS
    grid_size_km: float = 5.0
    target: str = "database"
    max_workers: int = DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS
    cell_workers: int = DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS
    db_batch_size: int = DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE
    high_volume: bool = DEFAULT_EARTH_ENGINE_HIGH_VOLUME


class CDSDroughtConfig(Config):
    lookback_months: int = 4
    include_ensemble_members: bool = True
    target: str = "database"


class KnmiConfig(Config):
    hours_back: int = 26
    target: str = "database"


class SeismicConfig(Config):
    days_back: int = 7
    target: str = "database"


# Shared retry policy for I/O-bound ingestion assets — most failures are
# transient (rate limits, intermittent 5xx) and recover within a minute.
_NETWORK_RETRY = RetryPolicy(
    max_retries=2,
    delay=60,
    backoff=Backoff.EXPONENTIAL,
)


def _stats_metadata(stats: object | None) -> dict[str, object]:
    """Return compact Dagster metadata from local ingestion stats objects."""
    if stats is None:
        return {}
    if hasattr(stats, "to_metadata"):
        return dict(stats.to_metadata())
    if hasattr(stats, "to_dict"):
        payload = stats.to_dict()
        if isinstance(payload, dict) and "results" in payload:
            result = dict(payload.get("results") or {})
            run = dict(payload.get("run") or {})
            for key in ("duration_seconds", "started_at", "finished_at"):
                if run.get(key) is not None:
                    result[key] = run[key]
            error_count = len(run.get("errors") or [])
            if error_count:
                result["errors"] = error_count
            return result
        if isinstance(payload, dict):
            return payload
    return {}


# ``op_tags['avian/deployment']`` keeps the QueuedRunCoordinator
# concurrency limit (per-deployment=1) effective for AMP and sensor runs,
# not just schedule-launched runs.


@asset(
    group_name="ingestion",
    op_tags={"avian/deployment": "birdweather-daily"},
    retry_policy=_NETWORK_RETRY,
)
def birdweather_raw_data(
    context: AssetExecutionContext,
    config: BirdweatherConfig,
) -> MaterializeResult:
    """Stage BirdWeather detections and raw audio into local date bundles."""
    stats = fetch_birdweather_data(
        hours=config.hours,
        country=config.country,
        species_ids=config.species_ids,
        all_species=config.all_species,
        min_confidence=config.min_confidence,
        workers=config.workers,
        target=config.target,
        download_audio=config.download_audio,
    )
    if config.target == "database":
        deduplicate_birdweather_detections()
    context.log.info(
        "BirdWeather local bundle staging materialized for %s.",
        config.country,
    )
    return MaterializeResult(
        metadata={
            "bundle_root": "data/raw/birdweather",
            "target": config.target,
            "country": config.country,
            "all_species": config.all_species,
            "download_audio": config.download_audio,
            **_stats_metadata(stats),
        }
    )


@asset(
    group_name="ingestion",
    op_tags={"avian/deployment": "knmi-daily"},
    retry_policy=_NETWORK_RETRY,
)
def knmi_raw_data(
    context: AssetExecutionContext,
    config: KnmiConfig,
) -> MaterializeResult:
    """Ingest KNMI weather station data."""
    fetch_knmi_data(
        target=config.target,
        hours_back=config.hours_back,
    )
    context.log.info("KNMI weather ingestion materialized.")
    return MaterializeResult(
        metadata={
            "table": "knmi_tbl",
            "target": config.target,
            "hours_back": config.hours_back,
        }
    )


@asset(
    group_name="ingestion",
    op_tags={"avian/deployment": "seismic-daily"},
    retry_policy=_NETWORK_RETRY,
)
def seismic_raw_data(
    context: AssetExecutionContext,
    config: SeismicConfig,
) -> MaterializeResult:
    """Ingest KNMI seismic data."""
    fetch_seismic_events(
        days_back=config.days_back,
        target=config.target,
    )
    context.log.info("KNMI seismic ingestion materialized.")
    return MaterializeResult(
        metadata={
            "table": "seismic_tbl",
            "target": config.target,
            "days_back": config.days_back,
        }
    )


@asset(
    group_name="ingestion",
    op_tags={"avian/deployment": "ebird-daily"},
    retry_policy=_NETWORK_RETRY,
)
def ebird_raw_data(
    context: AssetExecutionContext,
    config: EbirdConfig,
) -> MaterializeResult:
    """Ingest eBird observational data."""
    fetch_ebird_data(
        region=config.region,
        back=config.back,
        target_species_only=config.target_species_only,
        target=config.target,
    )
    context.log.info("eBird ingestion materialized for region %s.", config.region)
    return MaterializeResult(
        metadata={
            "table": "ebird_tbl",
            "target": config.target,
            "region": config.region,
            "back": config.back,
        }
    )


@asset(
    group_name="ingestion",
    op_tags={"avian/deployment": "movebank-daily"},
    retry_policy=_NETWORK_RETRY,
)
def movebank_raw_data(
    context: AssetExecutionContext,
    config: MovebankConfig,
) -> MaterializeResult:
    """Ingest Movebank animal tracking data."""
    stats = fetch_movebank_data(
        lookback_days=config.lookback_days,
        target=config.target,
    )
    context.log.info("Movebank ingestion materialized.")
    return MaterializeResult(
        metadata={
            "table": "movebank_tbl",
            "target": config.target,
            "lookback_days": config.lookback_days,
            **_stats_metadata(stats),
        }
    )


@asset(
    group_name="ingestion",
    op_tags={"avian/deployment": "earth-engine-daily"},
    retry_policy=_NETWORK_RETRY,
)
def earth_engine_raw_data(
    context: AssetExecutionContext,
    config: EarthEngineConfig,
) -> MaterializeResult:
    """Ingest Earth Engine point sampling data."""
    fetch_earth_engine_profiles(
        target=config.target,
        lookback_days=config.lookback_days,
        grid_size_km=config.grid_size_km,
        max_workers=config.max_workers,
        cell_workers=config.cell_workers,
        db_batch_size=config.db_batch_size,
        high_volume=config.high_volume,
    )
    context.log.info("Earth Engine point-profile ingestion materialized.")
    return MaterializeResult(
        metadata={
            "table": "google_earth_engine_tbl",
            "target": config.target,
            "lookback_days": config.lookback_days,
            "grid_size_km": config.grid_size_km,
            "max_workers": config.max_workers,
            "cell_workers": config.cell_workers,
            "db_batch_size": config.db_batch_size,
            "high_volume": config.high_volume,
        }
    )


@asset(
    group_name="ingestion",
    op_tags={"avian/deployment": "earth-engine-grid-daily"},
    retry_policy=_NETWORK_RETRY,
)
def earth_engine_grid_raw_data(
    context: AssetExecutionContext,
    config: EarthEngineConfig,
) -> MaterializeResult:
    """Ingest Earth Engine gridded data."""
    fetch_earth_engine_grid_profiles(
        target=config.target,
        lookback_days=config.lookback_days,
        grid_size_km=config.grid_size_km,
        max_workers=config.max_workers,
        cell_workers=config.cell_workers,
        db_batch_size=config.db_batch_size,
        high_volume=config.high_volume,
    )
    context.log.info("Earth Engine grid ingestion materialized.")
    return MaterializeResult(
        metadata={
            "table": "google_earth_engine_tbl",
            "target": config.target,
            "lookback_days": config.lookback_days,
            "grid_size_km": config.grid_size_km,
            "max_workers": config.max_workers,
            "cell_workers": config.cell_workers,
            "db_batch_size": config.db_batch_size,
            "high_volume": config.high_volume,
        }
    )


@asset(
    group_name="ingestion",
    op_tags={"avian/deployment": "cds-drought-daily"},
    retry_policy=_NETWORK_RETRY,
)
def cds_drought_raw_data(
    context: AssetExecutionContext,
    config: CDSDroughtConfig,
) -> MaterializeResult:
    """Ingest Copernicus Climate Data Store drought indicators."""
    fetch_cds_drought_profiles(
        target=config.target,
        lookback_months=config.lookback_months,
        include_ensemble_members=config.include_ensemble_members,
    )
    context.log.info("CDS drought ingestion materialized.")
    return MaterializeResult(
        metadata={
            "table": "cds_drought_tbl",
            "target": config.target,
            "lookback_months": config.lookback_months,
        }
    )


@asset(
    group_name="ingestion",
    op_tags={"avian/deployment": "sovon-daily"},
    retry_policy=_NETWORK_RETRY,
)
def sovon_raw_data(context: AssetExecutionContext) -> MaterializeResult:
    """Ingest Sovon bird counts."""
    run_sovon_flow()
    context.log.info("Sovon ingestion materialized.")
    return MaterializeResult(metadata={"table": "sovon_tbl", "target": "database"})


INGESTION_ASSETS = [
    birdweather_raw_data,
    knmi_raw_data,
    seismic_raw_data,
    ebird_raw_data,
    movebank_raw_data,
    earth_engine_raw_data,
    earth_engine_grid_raw_data,
    cds_drought_raw_data,
    sovon_raw_data,
]
