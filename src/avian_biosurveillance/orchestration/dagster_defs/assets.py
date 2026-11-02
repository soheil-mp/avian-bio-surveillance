"""Dagster assets for Postgres-backed post-processing tables.

These assets lift the existing maintenance flows into Dagster's asset graph
without changing the underlying SQL routines or database schema.
"""

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from dagster import (
    AssetExecutionContext,
    AssetKey,
    AssetSelection,
    AutoMaterializePolicy,
    Backoff,
    Config,
    Field,
    MaterializeResult,
    RetryPolicy,
    asset,
    define_asset_job,
)
from pydantic import Field as PydanticField

from avian_biosurveillance.orchestration.dagster_defs.code_locations import (
    CROSS_LOCATION_COMPOSITE_FLOW_KEYS,
    MODELING_FLOW_KEYS,
)
from avian_biosurveillance.orchestration.data_quality_flow import (
    run_quality_audit_task,
)
from avian_biosurveillance.orchestration.derived_aggregates_flow import (
    refresh_movebank_tracks_task,
    refresh_species_dim_task,
    refresh_species_tracker_task,
)
from avian_biosurveillance.orchestration.station_summary_flow import (
    refresh_station_summary_task,
)


_POSTGRES_ASSET_GROUP = "postgres_postprocessing"
_NETWORK_RETRY = RetryPolicy(
    max_retries=2,
    delay=60,
    backoff=Backoff.EXPONENTIAL,
)


class BirdweatherAudioProcessingConfig(Config):
    """Run config for BirdWeather processed-clip generation."""

    lookback_days: int = 2
    limit: int = 1_000
    force: bool = False


class BirdweatherRawAudioQueueConfig(Config):
    """Run config for BirdWeather raw-audio queue inspection."""

    lookback_days: int = 2
    limit: int = 5_000
    force: bool = False


class BirdweatherRawAudioDownloadConfig(Config):
    """Run config for BirdWeather raw soundscape download/upload."""

    lookback_days: int = 2
    limit: int = 1_000
    force: bool = False
    workers: int = 8


class BirdweatherBundleSyncConfig(Config):
    """Run config for syncing local BirdWeather bundles into DB/MinIO."""

    limit: int = 1
    delete_on_success: bool = True
    output_dir: str = "data/raw/birdweather"
    bundle_dates: list[str] = PydanticField(default_factory=list)


class BirdweatherFeatureEngineeringConfig(Config):
    """Run config for BirdWeather audio feature generation."""

    lookback_days: int = 2
    limit: int = 500
    force: bool = False
    background_device: str = ""
    background_top_k: int = 5
    background_min_confidence: float = 0.10


class ModelAnomalyScoringConfig(Config):
    """Run config for the first-iteration modeling anomaly scorer."""

    seasonal_window_weeks: int = 2
    min_location_history: int = 20
    min_seasonal_history: int = 8
    min_observation_days: int = 3
    target_species_only: bool = True
    include_embedding_drift: bool = True
    embedding_min_confidence: float = 0.1
    embedding_min_baseline_clips: int = 50
    embedding_min_week_clips: int = 3
    embedding_k_neighbors: int = 10
    embedding_cluster_min_samples: int = 2
    embedding_cluster_distance_multiplier: float = 1.2
    embedding_cluster_distance_floor: float = 0.03
    embedding_cluster_distance_ceiling: float = 0.18
    anomaly_min_model_rows: int = 20
    anomaly_contamination: float = 0.05
    anomaly_random_state: int = 42
    amber_quantile: float = 0.95
    red_quantile: float = 0.99


class ModelTuningSweepConfig(Config):
    """Run config for the on-demand anomaly-model tuning sweep."""

    config_path: str | None = None


def _database_name() -> str:
    return os.environ.get(
        "STATION_SUMMARY_DB_NAME",
        os.environ.get("POSTGRES_DB", "avian_biosurveillance"),
    )


def _asset_metadata(
    *,
    tables: list[str],
    routine: str,
    params: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "database": _database_name(),
        "tables": ", ".join(tables),
        "routine": routine,
    }
    if params:
        metadata["parameters"] = json.dumps(params, sort_keys=True, default=str)
    return metadata


@asset(
    name="recording_station_summary",
    group_name=_POSTGRES_ASSET_GROUP,
    deps=[AssetKey("birdweather_synced_bundles")],
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    op_tags={"avian/deployment": "recording-station-summary"},
    description=(
        "Refreshes birdweather_daily_cagg_tbl and then recomputes "
        "birdweather_stations_tbl via refresh_station_summary()."
    ),
)
def recording_station_summary(context: AssetExecutionContext) -> MaterializeResult[Any]:
    """Materialize the per-station aggregate table in PostgreSQL."""
    context.log.info(
        "Materializing asset 'recording_station_summary' in database '%s'.",
        _database_name(),
    )
    refresh_station_summary_task()
    return MaterializeResult(
        metadata=_asset_metadata(
            tables=["birdweather_daily_cagg_tbl", "birdweather_stations_tbl"],
            routine="refresh_station_summary()",
        )
    )


@asset(
    name="species_dimension",
    group_name=_POSTGRES_ASSET_GROUP,
    deps=[
        AssetKey("birdweather_synced_bundles"),
        AssetKey("ebird_raw_data"),
        AssetKey("sovon_raw_data"),
    ],
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    op_tags={"avian/deployment": "species-dimension"},
    description="Rebuilds species_tbl via refresh_species_dim().",
)
def species_dimension(context: AssetExecutionContext) -> MaterializeResult[Any]:
    """Materialize the species dimension table in PostgreSQL."""
    context.log.info(
        "Materializing asset 'species_dimension' in database '%s'.",
        _database_name(),
    )
    refresh_species_dim_task()
    return MaterializeResult(
        metadata=_asset_metadata(
            tables=["species_tbl"],
            routine="refresh_species_dim()",
        )
    )


@asset(
    name="movebank_tracks",
    group_name=_POSTGRES_ASSET_GROUP,
    deps=[AssetKey("movebank_raw_data")],
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    config_schema={
        "max_points": Field(
            int,
            is_required=False,
            default_value=500,
            description="Soft cap on coordinates per individual.",
        )
    },
    op_tags={"avian/deployment": "movebank-tracks"},
    description="Rebuilds movebank_tracks_tbl via refresh_movebank_tracks(max_points).",
)
def movebank_tracks(context: AssetExecutionContext) -> MaterializeResult[Any]:
    """Materialize the dashboard-ready Movebank track table."""
    max_points = int(context.op_execution_context.op_config.get("max_points", 500))
    context.log.info(
        "Materializing asset 'movebank_tracks' in database '%s' with max_points=%d.",
        _database_name(),
        max_points,
    )
    refresh_movebank_tracks_task(max_points=max_points)
    return MaterializeResult(
        metadata=_asset_metadata(
            tables=["movebank_tracks_tbl"],
            routine="refresh_movebank_tracks(max_points)",
            params={"max_points": max_points},
        )
    )


@asset(
    name="species_tracker",
    group_name=_POSTGRES_ASSET_GROUP,
    deps=[
        AssetKey("recording_station_summary"),
        AssetKey("species_dimension"),
        AssetKey("birdweather_feature_engineering"),
        AssetKey("knmi_raw_data"),
        AssetKey("earth_engine_raw_data"),
        AssetKey("earth_engine_grid_raw_data"),
        AssetKey("cds_drought_raw_data"),
        AssetKey("seismic_raw_data"),
    ],
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    op_tags={"avian/deployment": "species-tracker"},
    description=(
        "Rebuilds species_tracker_tbl via refresh_species_tracker() using "
        "BirdWeather, KNMI, GEE, CDS drought, and seismic sources."
    ),
)
def species_tracker(context: AssetExecutionContext) -> MaterializeResult[Any]:
    """Materialize the unified species tracker table."""
    context.log.info(
        "Materializing asset 'species_tracker' in database '%s'.",
        _database_name(),
    )
    refresh_species_tracker_task()
    return MaterializeResult(
        metadata=_asset_metadata(
            tables=["species_tracker_tbl"],
            routine="refresh_species_tracker()",
        )
    )


@asset(
    name="birdweather_synced_bundles",
    group_name="ingestion",
    deps=[AssetKey("birdweather_raw_data")],
    retry_policy=_NETWORK_RETRY,
    op_tags={"avian/deployment": "birdweather-bundle-sync"},
    description=(
        "Loads pending local BirdWeather date bundles from data/raw/birdweather, "
        "upserts detections into TimescaleDB, uploads local soundscapes into "
        "MinIO raw-audio, deduplicates the table, and deletes successfully "
        "synced local bundles."
    ),
)
def birdweather_synced_bundles(
    context: AssetExecutionContext,
    config: BirdweatherBundleSyncConfig,
) -> MaterializeResult[Any]:
    """Materialize pending local BirdWeather bundles into DB/MinIO."""
    from avian_biosurveillance.ingestion.birdweather import AudioCollector
    from avian_biosurveillance.orchestration.birdweather_flow import (
        deduplicate_birdweather_detections,
    )

    collector = AudioCollector(Path(config.output_dir))
    stats = collector.sync_local_bundles_to_database(
        limit=config.limit if config.limit > 0 else None,
        bundle_dates=(list(config.bundle_dates) if config.bundle_dates else None),
        delete_on_success=config.delete_on_success,
    )
    if stats.detections_upserted:
        deduplicate_birdweather_detections()
    context.log.info(
        "BirdWeather bundle sync: %d bundle(s) synced, %d detection(s) upserted, %d audio uploaded.",
        stats.bundles_synced,
        stats.detections_upserted,
        stats.audio_uploaded,
    )
    return MaterializeResult(
        metadata={
            "table": "birdweather_tbl",
            "bucket": "raw-audio",
            "output_dir": config.output_dir,
            **stats.to_metadata(),
        }
    )


@asset(
    name="birdweather_audio_download_queue",
    group_name="audio_ingestion",
    deps=[AssetKey("birdweather_synced_bundles")],
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    retry_policy=_NETWORK_RETRY,
    op_tags={"avian/deployment": "birdweather-raw-audio-ingestion"},
    description=(
        "Counts BirdWeather detections with audio URLs whose raw soundscape "
        "object is not yet present in MinIO."
    ),
)
def birdweather_audio_download_queue(
    context: AssetExecutionContext,
    config: BirdweatherRawAudioQueueConfig,
) -> MaterializeResult[Any]:
    """Materialize the query-backed raw-audio download queue."""
    from avian_biosurveillance.db.minio_client import BUCKET_RAW_AUDIO
    from avian_biosurveillance.processing.birdweather_raw_audio import (
        inspect_birdweather_raw_audio_queue,
    )

    stats = inspect_birdweather_raw_audio_queue(
        lookback_days=config.lookback_days if config.lookback_days > 0 else None,
        limit=config.limit if config.limit > 0 else None,
        force=config.force,
    )
    context.log.info(
        "BirdWeather raw-audio queue: %d queued, %d already present.",
        stats.queued,
        stats.already_present,
    )
    return MaterializeResult(
        metadata={
            "bucket": BUCKET_RAW_AUDIO,
            "table": "birdweather_tbl",
            **stats.to_metadata(),
        }
    )


@asset(
    name="birdweather_raw_audio_objects",
    group_name="audio_ingestion",
    deps=[AssetKey("birdweather_audio_download_queue")],
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    retry_policy=_NETWORK_RETRY,
    op_tags={"avian/deployment": "birdweather-raw-audio-ingestion"},
    description=(
        "Downloads queued BirdWeather soundscapes and stores raw FLAC/WAV/MP3 "
        "objects in MinIO raw-audio."
    ),
)
def birdweather_raw_audio_objects(
    context: AssetExecutionContext,
    config: BirdweatherRawAudioDownloadConfig,
) -> MaterializeResult[Any]:
    """Materialize raw BirdWeather soundscape objects in MinIO."""
    from avian_biosurveillance.db.minio_client import BUCKET_RAW_AUDIO
    from avian_biosurveillance.processing.birdweather_raw_audio import (
        download_birdweather_raw_audio_objects,
    )

    stats = download_birdweather_raw_audio_objects(
        lookback_days=config.lookback_days if config.lookback_days > 0 else None,
        limit=config.limit if config.limit > 0 else None,
        force=config.force,
        workers=config.workers,
    )
    context.log.info(
        "BirdWeather raw audio: %d uploaded, %d already present, %d failed.",
        stats.uploaded,
        stats.already_present,
        stats.failed + stats.upload_failed,
    )
    return MaterializeResult(
        metadata={
            "bucket": BUCKET_RAW_AUDIO,
            "table": "birdweather_tbl",
            **stats.to_metadata(),
        }
    )


@asset(
    name="data_quality_audit_results",
    group_name="postgres_quality",
    deps=[
        AssetKey("birdweather_synced_bundles"),
        AssetKey("knmi_raw_data"),
        AssetKey("seismic_raw_data"),
        AssetKey("ebird_raw_data"),
        AssetKey("earth_engine_raw_data"),
        AssetKey("earth_engine_grid_raw_data"),
        AssetKey("cds_drought_raw_data"),
        AssetKey("sovon_raw_data"),
    ],
    op_tags={"avian/deployment": "quality-audit"},
    description=(
        "Runs the ISO 25012 audit and persists results to data_quality_tbl. "
        "The daily-quality sensor launches this asset job per logical date "
        "once the full daily cohort has succeeded."
    ),
)
def data_quality_audit_results(
    context: AssetExecutionContext,
) -> MaterializeResult[Any]:
    """Materialize the persisted data-quality audit results."""
    context.log.info(
        "Materializing asset 'data_quality_audit_results' in database '%s'.",
        _database_name(),
    )
    run_quality_audit_task()
    return MaterializeResult(
        metadata=_asset_metadata(
            tables=["data_quality_tbl"],
            routine="store_results(report, station_metrics)",
        )
    )


@asset(
    name="birdweather_audio_processing",
    group_name="audio_processing",
    deps=[AssetKey("birdweather_raw_audio_objects")],
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    retry_policy=_NETWORK_RETRY,
    op_tags={"avian/deployment": "birdweather-audio-processing"},
    description=(
        "Crops BirdWeather raw soundscapes into mono detection clips after "
        "raw audio objects have landed, then stores them in MinIO processed-audio."
    ),
)
def birdweather_audio_processing(
    context: AssetExecutionContext,
    config: BirdweatherAudioProcessingConfig,
) -> MaterializeResult[Any]:
    """Materialize cropped BirdWeather audio clips."""
    from avian_biosurveillance.processing.birdweather_audio import (
        process_birdweather_audio_clips,
    )

    context.log.info("Materializing BirdWeather processed audio clips.")
    stats = process_birdweather_audio_clips(
        lookback_days=config.lookback_days if config.lookback_days > 0 else None,
        limit=config.limit if config.limit > 0 else None,
        force=config.force,
        storage_retry_attempts=2,
    )
    return MaterializeResult(
        metadata={
            "bucket": "processed-audio",
            **stats.to_metadata(),
        }
    )


@asset(
    name="birdweather_feature_engineering",
    group_name="audio_features",
    deps=[AssetKey("birdweather_audio_processing")],
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    retry_policy=_NETWORK_RETRY,
    op_tags={"avian/deployment": "birdweather-feature-engineering"},
    description=(
        "Generates BirdWeather spectrograms, waveforms, embeddings, and "
        "background-sound classifications from processed clips."
    ),
)
def birdweather_feature_engineering(
    context: AssetExecutionContext,
    config: BirdweatherFeatureEngineeringConfig,
) -> MaterializeResult[Any]:
    """Materialize BirdWeather feature artifacts and DB enrichments."""
    from avian_biosurveillance.features.birdweather_features import (
        engineer_birdweather_audio_features,
    )

    context.log.info("Materializing BirdWeather audio features.")
    stats = engineer_birdweather_audio_features(
        lookback_days=config.lookback_days if config.lookback_days > 0 else None,
        limit=config.limit if config.limit > 0 else None,
        force=config.force,
        background_device=config.background_device or None,
        background_top_k=config.background_top_k,
        background_min_confidence=config.background_min_confidence,
    )
    return MaterializeResult(
        metadata={
            "bucket": "features",
            "spectrogram_prefix": "spectrograms/",
            "waveform_prefix": "waveforms/",
            **stats.to_metadata(),
        }
    )


@asset(
    name="model_anomaly_scores",
    group_name="modeling",
    deps=[
        AssetKey("species_tracker"),
        AssetKey("birdweather_feature_engineering"),
    ],
    auto_materialize_policy=AutoMaterializePolicy.eager(),
    retry_policy=_NETWORK_RETRY,
    op_tags={"avian/deployment": "model-anomaly-scoring"},
    description=(
        "Scores first-iteration weekly species/location anomalies from "
        "species_tracker_tbl and persists reproducible modeling outputs in Postgres."
    ),
)
def model_anomaly_scores(
    context: AssetExecutionContext,
    config: ModelAnomalyScoringConfig,
) -> MaterializeResult[Any]:
    """Materialize weekly anomaly scores for the modeling phase."""
    from avian_biosurveillance.modeling.pipeline import run_model_anomaly_scoring

    context.log.info("Materializing first-iteration modeling anomaly scores.")
    stats = run_model_anomaly_scoring(
        seasonal_window_weeks=config.seasonal_window_weeks,
        min_location_history=config.min_location_history,
        min_seasonal_history=config.min_seasonal_history,
        min_observation_days=config.min_observation_days,
        target_species_only=config.target_species_only,
        include_embedding_drift=config.include_embedding_drift,
        embedding_min_confidence=config.embedding_min_confidence,
        embedding_min_baseline_clips=config.embedding_min_baseline_clips,
        embedding_min_week_clips=config.embedding_min_week_clips,
        embedding_k_neighbors=config.embedding_k_neighbors,
        embedding_cluster_min_samples=config.embedding_cluster_min_samples,
        embedding_cluster_distance_multiplier=(
            config.embedding_cluster_distance_multiplier
        ),
        embedding_cluster_distance_floor=config.embedding_cluster_distance_floor,
        embedding_cluster_distance_ceiling=config.embedding_cluster_distance_ceiling,
        anomaly_min_model_rows=config.anomaly_min_model_rows,
        anomaly_contamination=config.anomaly_contamination,
        anomaly_random_state=config.anomaly_random_state,
        amber_quantile=config.amber_quantile,
        red_quantile=config.red_quantile,
    )
    return MaterializeResult(metadata=stats.to_metadata())


@asset(
    name="model_tuning_sweep",
    group_name="modeling",
    deps=[
        AssetKey("species_tracker"),
        AssetKey("birdweather_feature_engineering"),
    ],
    retry_policy=_NETWORK_RETRY,
    op_tags={"avian/deployment": "model-tuning-sweep"},
    description=(
        "On-demand tuning sweep that reruns anomaly scoring across a grid of "
        "unsupervised knobs and ranks configurations by weak-label alignment "
        "(SOVON mortality). The model is never trained on labels; "
        "alignment is used only for selection. Persists a leaderboard in Postgres."
    ),
)
def model_tuning_sweep(
    context: AssetExecutionContext,
    config: ModelTuningSweepConfig,
) -> MaterializeResult[Any]:
    """Materialize a ranked tuning leaderboard for model selection."""
    from avian_biosurveillance.modeling.tuning import load_tuning_config, run_sweep

    sweep_config = load_tuning_config(config.config_path)
    context.log.info(
        "Running tuning sweep (mode=%s, max_configs=%d).",
        sweep_config.mode,
        sweep_config.max_configs,
    )
    leaderboard = run_sweep(sweep_config)

    metadata: dict[str, Any] = {
        "config_count": len(leaderboard),
        "mode": sweep_config.mode,
    }
    if not leaderboard.empty:
        best = leaderboard.iloc[0]
        metadata.update(
            {
                "best_config_label": str(best["config_label"]),
                "best_objective": float(best["objective"]),
                "best_config_overrides": str(best["overrides"]),
            }
        )
    return MaterializeResult(metadata=metadata)


OPERATIONAL_POSTPROCESSING_ASSETS = [
    birdweather_synced_bundles,
    recording_station_summary,
    species_dimension,
    movebank_tracks,
    species_tracker,
    birdweather_audio_download_queue,
    birdweather_raw_audio_objects,
    data_quality_audit_results,
    birdweather_audio_processing,
]

MODELING_ASSETS = [
    birdweather_feature_engineering,
    model_anomaly_scores,
    model_tuning_sweep,
]

POSTPROCESSING_ASSETS = [
    *OPERATIONAL_POSTPROCESSING_ASSETS,
    *MODELING_ASSETS,
]


POSTPROCESSING_ASSET_JOBS = {
    "birdweather_bundle_sync": define_asset_job(
        name="birdweather_bundle_sync_job",
        selection=AssetSelection.assets(birdweather_synced_bundles),
        description="Materialize pending local BirdWeather bundles into DB/MinIO.",
        tags={
            "avian/flow_key": "birdweather_bundle_sync",
            "avian/orchestration_kind": "asset_job",
        },
    ),
    "station_summary_refresh": define_asset_job(
        name="station_summary_refresh",
        selection=AssetSelection.assets(recording_station_summary),
        description="Materialize the recording_station_summary asset.",
        tags={
            "avian/flow_key": "station_summary_refresh",
            "avian/orchestration_kind": "asset_job",
        },
    ),
    "derived_aggregates_refresh": define_asset_job(
        name="derived_aggregates_refresh",
        selection=AssetSelection.assets(
            species_dimension,
            movebank_tracks,
            species_tracker,
        ),
        description="Materialize the derived Postgres dashboard assets.",
        tags={
            "avian/flow_key": "derived_aggregates_refresh",
            "avian/orchestration_kind": "asset_job",
        },
    ),
    "quality_audit": define_asset_job(
        name="quality_audit",
        selection=AssetSelection.assets(data_quality_audit_results),
        description="Materialize the persisted data-quality audit asset.",
        tags={
            "avian/flow_key": "quality_audit",
            "avian/orchestration_kind": "asset_job",
        },
    ),
    "birdweather_raw_audio_ingestion": define_asset_job(
        name="birdweather_raw_audio_ingestion_job",
        selection=AssetSelection.assets(
            birdweather_audio_download_queue,
            birdweather_raw_audio_objects,
        ),
        description="Materialize BirdWeather raw-audio queue and MinIO objects.",
        tags={
            "avian/flow_key": "birdweather_raw_audio_ingestion",
            "avian/orchestration_kind": "asset_job",
        },
    ),
    "birdweather_audio_processing": define_asset_job(
        name="birdweather_audio_processing_job",
        selection=AssetSelection.assets(birdweather_audio_processing),
        description="Materialize cropped BirdWeather audio clips.",
        tags={
            "avian/flow_key": "birdweather_audio_processing",
            "avian/orchestration_kind": "asset_job",
        },
    ),
    "birdweather_feature_engineering": define_asset_job(
        name="birdweather_feature_engineering_job",
        selection=AssetSelection.assets(birdweather_feature_engineering),
        description="Materialize BirdWeather audio feature artifacts.",
        tags={
            "avian/flow_key": "birdweather_feature_engineering",
            "avian/orchestration_kind": "asset_job",
        },
    ),
    "model_anomaly_scoring": define_asset_job(
        name="model_anomaly_scoring_job",
        selection=AssetSelection.assets(model_anomaly_scores),
        description="Materialize first-iteration weekly modeling anomaly scores.",
        tags={
            "avian/flow_key": "model_anomaly_scoring",
            "avian/orchestration_kind": "asset_job",
        },
    ),
    "model_tuning_sweep": define_asset_job(
        name="model_tuning_sweep_job",
        selection=AssetSelection.assets(model_tuning_sweep),
        description=(
            "On-demand tuning sweep ranking anomaly-model configs by "
            "weak-label alignment."
        ),
        tags={
            "avian/flow_key": "model_tuning_sweep",
            "avian/orchestration_kind": "asset_job",
        },
    ),
    "birdweather_audio_feature_pipeline": define_asset_job(
        name="birdweather_audio_feature_pipeline",
        selection=AssetSelection.assets(
            birdweather_audio_download_queue,
            birdweather_raw_audio_objects,
            birdweather_audio_processing,
            birdweather_feature_engineering,
        ),
        description="Materialize BirdWeather raw audio, processing, and feature assets.",
        tags={
            "avian/flow_key": "birdweather_audio_feature_pipeline",
            "avian/orchestration_kind": "asset_job",
        },
    ),
}

MODELING_ASSET_JOBS = {
    key: job
    for key, job in POSTPROCESSING_ASSET_JOBS.items()
    if key in MODELING_FLOW_KEYS
}

OPERATIONAL_POSTPROCESSING_ASSET_JOBS = {
    key: job
    for key, job in POSTPROCESSING_ASSET_JOBS.items()
    if key not in MODELING_FLOW_KEYS | CROSS_LOCATION_COMPOSITE_FLOW_KEYS
}


def build_asset_job_run_config(
    flow_key: str,
    params: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return Dagster run-config for asset-backed flows when needed."""
    if flow_key == "birdweather_bundle_sync":
        return _single_asset_run_config(
            "birdweather_synced_bundles",
            params,
            {"limit", "delete_on_success", "output_dir", "bundle_dates"},
        )

    if flow_key == "birdweather_raw_audio_ingestion":
        return {
            "ops": {
                "birdweather_audio_download_queue": {
                    "config": _filtered_config(
                        params,
                        {"lookback_days", "limit", "force"},
                    ),
                },
                "birdweather_raw_audio_objects": {
                    "config": _filtered_config(
                        params,
                        {"lookback_days", "limit", "force", "workers"},
                    ),
                },
            }
        }

    if flow_key == "birdweather_audio_processing":
        return _single_asset_run_config(
            "birdweather_audio_processing",
            params,
            {"lookback_days", "limit", "force"},
        )

    if flow_key == "birdweather_feature_engineering":
        return _single_asset_run_config(
            "birdweather_feature_engineering",
            params,
            {
                "lookback_days",
                "limit",
                "force",
                "background_device",
                "background_top_k",
                "background_min_confidence",
            },
        )

    if flow_key == "model_anomaly_scoring":
        return _single_asset_run_config(
            "model_anomaly_scores",
            params,
            {
                "seasonal_window_weeks",
                "min_location_history",
                "min_seasonal_history",
                "min_observation_days",
                "target_species_only",
                "include_embedding_drift",
                "embedding_min_confidence",
                "embedding_min_baseline_clips",
                "embedding_min_week_clips",
                "embedding_k_neighbors",
                "embedding_cluster_min_samples",
                "embedding_cluster_distance_multiplier",
                "embedding_cluster_distance_floor",
                "embedding_cluster_distance_ceiling",
                "anomaly_min_model_rows",
                "anomaly_contamination",
                "anomaly_random_state",
                "amber_quantile",
                "red_quantile",
            },
        )

    if flow_key == "birdweather_audio_feature_pipeline":
        return {
            "ops": {
                "birdweather_audio_download_queue": {
                    "config": _filtered_config(
                        params,
                        {"lookback_days", "limit", "force"},
                    ),
                },
                "birdweather_raw_audio_objects": {
                    "config": _filtered_config(
                        params,
                        {"lookback_days", "limit", "force", "workers"},
                    ),
                },
                "birdweather_audio_processing": {
                    "config": _filtered_config(
                        params,
                        {"lookback_days", "limit", "force"},
                    ),
                },
                "birdweather_feature_engineering": {
                    "config": _filtered_config(
                        params,
                        {
                            "lookback_days",
                            "limit",
                            "force",
                            "background_device",
                            "background_top_k",
                            "background_min_confidence",
                        },
                    ),
                },
            }
        }

    if flow_key != "derived_aggregates_refresh":
        return {} if flow_key in POSTPROCESSING_ASSET_JOBS else None

    max_points = int(params.get("max_points", 500))
    return {
        "ops": {
            "movebank_tracks": {
                "config": {"max_points": max_points},
            }
        }
    }


def _single_asset_run_config(
    asset_name: str,
    params: Mapping[str, Any],
    allowed_keys: set[str],
) -> dict[str, Any]:
    config = _filtered_config(params, allowed_keys)
    if not config:
        return {}
    return {"ops": {asset_name: {"config": config}}}


def _filtered_config(
    params: Mapping[str, Any],
    allowed_keys: set[str],
) -> dict[str, Any]:
    return {
        key: value
        for key, value in params.items()
        if key in allowed_keys and value is not None
    }


ASSET_BACKED_FLOW_KEYS = set(POSTPROCESSING_ASSET_JOBS)


__all__ = [
    "ASSET_BACKED_FLOW_KEYS",
    "MODELING_ASSETS",
    "MODELING_ASSET_JOBS",
    "OPERATIONAL_POSTPROCESSING_ASSETS",
    "OPERATIONAL_POSTPROCESSING_ASSET_JOBS",
    "POSTPROCESSING_ASSETS",
    "POSTPROCESSING_ASSET_JOBS",
    "build_asset_job_run_config",
]
