"""Daily modeling pipeline for avian anomaly scores."""

from __future__ import annotations

import gc
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from time import perf_counter
from typing import Any

import pandas as pd
import psycopg2.extras  # type: ignore[import-untyped]
import psycopg2.sql  # type: ignore[import-untyped]

from avian_biosurveillance.config.species import registry as species_registry
from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.loaders.modeling import persist_modeling_run
from avian_biosurveillance.modeling.anomaly_model import (
    BehaviorAnomalyModelConfig,
    score_behavior_anomalies,
    score_behavior_anomalies_walk_forward,
)
from avian_biosurveillance.modeling.count_baseline import (
    PopulationBaselineConfig,
    score_population_anomalies,
)
from avian_biosurveillance.modeling.embedding_drift import (
    EmbeddingDriftConfig,
    attach_embedding_control,
    parse_embedding_vector,
    score_embedding_clusters,
    score_embedding_drift,
    score_embedding_scene_control,
)
from avian_biosurveillance.modeling.environmental_context import (
    ENVIRONMENTAL_CONTEXT_COLUMNS,
)
from avian_biosurveillance.modeling.evaluation import build_validation_report
from avian_biosurveillance.modeling.feature_set import build_species_week_feature_set
from avian_biosurveillance.modeling.schemas import ModelingRunStats
from avian_biosurveillance.modeling.settings import SETTINGS
from avian_biosurveillance.utils.progress import tqdm


logger = logging.getLogger(__name__)
_EMBEDDING_FETCH_BATCH_SIZE = 5_000


@contextmanager
def _timed_phase(label: str) -> Iterator[None]:
    """Log elapsed wall time for a modeling phase."""
    started = perf_counter()
    logger.info("%s started.", label)
    try:
        yield
    finally:
        logger.info("%s finished in %.2fs.", label, perf_counter() - started)


def _normalise_species_ids(
    species_ids: tuple[str, ...] | list[str] | None,
) -> tuple[str, ...] | None:
    if species_ids is None:
        return None
    cleaned = tuple(
        dict.fromkeys(
            str(species_id).strip()
            for species_id in species_ids
            if str(species_id).strip()
        )
    )
    return cleaned or None


def _effective_target_species_ids(
    *,
    target_species_only: bool,
    species_ids: tuple[str, ...] | list[str] | None = None,
) -> tuple[str, ...] | None:
    explicit_species_ids = _normalise_species_ids(species_ids)
    if explicit_species_ids is not None:
        return explicit_species_ids
    if target_species_only:
        return tuple(str(value) for value in species_registry.birdweather_ids)
    return None


def _sovon_filter_values_for_species_ids(
    species_ids: tuple[str, ...],
) -> tuple[list[int], list[str]]:
    english_names: list[str] = []
    sovon_ids: list[int] = []
    for species_id in species_ids:
        english = species_registry.english_by_birdweather_id.get(str(species_id))
        if not english:
            continue
        english_names.append(english)
        sovon_id = species_registry.sovon_id_by_english.get(english)
        if sovon_id is not None:
            sovon_ids.append(int(sovon_id))
    return sovon_ids, english_names


def run_model_anomaly_scoring(
    *,
    seasonal_window_weeks: int = SETTINGS.feature_set["seasonal_window_weeks"],
    min_location_history: int = SETTINGS.feature_set["min_location_history"],
    min_seasonal_history: int = SETTINGS.feature_set["min_seasonal_history"],
    min_observation_days: int = SETTINGS.feature_set["min_observation_days"],
    min_station_availability_ratio: float = float(
        SETTINGS.feature_set.get("min_station_availability_ratio", 0.5)
    ),
    min_station_baseline_history: int = int(
        SETTINGS.feature_set.get("min_station_baseline_history", 4)
    ),
    population_activity_components: tuple[str, ...] = tuple(
        SETTINGS.feature_set.get(
            "population_activity_components",
            (
                "relative_abundance_pct",
                "temporal_occupancy_pct",
                "station_presence_pct",
                "detections_per_active_station_day",
            ),
        )
    ),
    target_species_only: bool = SETTINGS.feature_set["target_species_only"],
    species_ids: tuple[str, ...] | list[str] | None = None,
    include_embedding_drift: bool = SETTINGS.embedding_drift["include_embedding_drift"],
    include_embedding_clusters: bool = SETTINGS.embedding_drift[
        "include_embedding_drift"
    ],
    include_embedding_control: bool = bool(
        SETTINGS.embedding_drift.get("include_embedding_control", True)
    ),
    embedding_min_control_species: int = int(
        SETTINGS.embedding_drift.get("min_control_species", 1)
    ),
    embedding_min_confidence: float = SETTINGS.embedding_drift["min_confidence"],
    embedding_min_baseline_clips: int = SETTINGS.embedding_drift["min_baseline_clips"],
    embedding_min_week_clips: int = SETTINGS.embedding_drift["min_week_clips"],
    embedding_k_neighbors: int = SETTINGS.embedding_drift["k_neighbors"],
    embedding_cluster_min_samples: int = int(
        SETTINGS.embedding_drift.get("cluster_min_samples", 2)
    ),
    embedding_cluster_distance_multiplier: float = float(
        SETTINGS.embedding_drift.get("cluster_distance_multiplier", 1.2)
    ),
    embedding_cluster_distance_floor: float = float(
        SETTINGS.embedding_drift.get("cluster_distance_floor", 0.03)
    ),
    embedding_cluster_distance_ceiling: float = float(
        SETTINGS.embedding_drift.get("cluster_distance_ceiling", 0.18)
    ),
    anomaly_min_model_rows: int = SETTINGS.anomaly_model["min_model_rows"],
    anomaly_contamination: float = SETTINGS.anomaly_model["contamination"],
    anomaly_random_state: int = SETTINGS.anomaly_model["random_state"],
    amber_quantile: float = SETTINGS.anomaly_model["amber_quantile"],
    red_quantile: float = SETTINGS.anomaly_model["red_quantile"],
    include_environmental: bool = bool(
        SETTINGS.anomaly_model.get("include_environmental", False)
    ),
    model_streams: tuple[str, ...] = tuple(
        SETTINGS.anomaly_model.get("model_streams", ("population", "bioacoustic"))
    ),
    scoring_mode: str = "retrospective",
    include_sovon_context: bool = True,
    skip_evaluation: bool = False,
    parent_tuning_run_id: str | None = None,
    source_start_date: date | str | None = None,
    source_end_date: date | str | None = None,
    output_period_start: date | str | None = None,
    output_period_end: date | str | None = None,
    run_id: str | None = None,
    run_status: str = "complete",
    replace_existing_run: bool = True,
    append_only: bool = False,
    preloaded_embedding_clip_rows: pd.DataFrame | None = None,
    preloaded_control_embedding_clip_rows: pd.DataFrame | None = None,
    pool: ConnectionPool | None = None,
) -> ModelingRunStats:
    """Build weekly grid-based modeling features and behavior anomaly scores.

    The scoring model is unsupervised and uses BirdWeather behavior streams.
    SOVON mortality data is attached only as a context/evaluation signal.
    """
    logger.info(
        "Starting model anomaly scoring: scoring_mode=%s target_species_only=%s "
        "include_embedding_drift=%s include_embedding_clusters=%s.",
        scoring_mode,
        target_species_only,
        include_embedding_drift,
        include_embedding_clusters,
    )
    own_pool = pool is None
    pool = pool or ConnectionPool(min_conn=1, max_conn=3)
    run_started = perf_counter()
    normalized_source_start = _coerce_date(source_start_date)
    normalized_source_end = _coerce_date(source_end_date)
    normalized_output_start = _coerce_date(output_period_start)
    normalized_output_end = _coerce_date(output_period_end)
    selected_species_ids = _effective_target_species_ids(
        target_species_only=target_species_only,
        species_ids=species_ids,
    )
    _validate_optional_window(
        normalized_source_start,
        normalized_source_end,
        label="source",
    )
    _validate_optional_window(
        normalized_output_start,
        normalized_output_end,
        label="output",
    )
    try:
        with _timed_phase("Load species_tracker modeling rows"):
            rows = _load_species_week_rows(
                pool,
                target_species_only=target_species_only,
                species_ids=selected_species_ids,
                period_start=normalized_source_start,
                period_end=normalized_source_end,
            )
        _log_weekly_frame_coverage(
            "source species-tracker weeks",
            rows,
            list_periods=True,
        )
        if include_sovon_context:
            with _timed_phase("Load SOVON mortality context"):
                sovon_rows = _load_sovon_month_rows(
                    pool,
                    target_species_only=target_species_only,
                    species_ids=selected_species_ids,
                )
        else:
            logger.info("Load SOVON mortality context skipped by configuration.")
            sovon_rows = pd.DataFrame()
        embedding_config = EmbeddingDriftConfig(
            seasonal_window_weeks=seasonal_window_weeks,
            min_baseline_clips=embedding_min_baseline_clips,
            min_week_clips=embedding_min_week_clips,
            k_neighbors=embedding_k_neighbors,
            min_confidence=embedding_min_confidence,
            cluster_min_samples=embedding_cluster_min_samples,
            cluster_distance_multiplier=embedding_cluster_distance_multiplier,
            cluster_distance_floor=embedding_cluster_distance_floor,
            cluster_distance_ceiling=embedding_cluster_distance_ceiling,
        )
        if not include_embedding_drift:
            embedding_clip_rows = pd.DataFrame()
            embedding_weekly = pd.DataFrame()
            scene_control = pd.DataFrame()
            embedding_clusters = None
        elif preloaded_embedding_clip_rows is None:
            # Memory-safe path: stream the embedding stage one species at a time
            # so we never hold every clip in memory at once. Results are identical
            # to the all-at-once path because drift/clusters are grouped within
            # species and the embedding baseline is built within species.
            with _timed_phase("Score embedding stage (streamed per species)"):
                (
                    embedding_weekly,
                    embedding_clusters,
                    scene_control,
                    embedding_clip_rows,
                ) = _compute_embedding_stage_streamed(
                    pool,
                    selected_species_ids=selected_species_ids,
                    config=embedding_config,
                    embedding_min_confidence=embedding_min_confidence,
                    include_embedding_clusters=include_embedding_clusters,
                    include_embedding_control=include_embedding_control,
                    embedding_min_control_species=embedding_min_control_species,
                    source_start=normalized_source_start,
                    source_end=normalized_source_end,
                    output_start=normalized_output_start,
                    output_end=normalized_output_end,
                )
        else:
            # Preloaded path (caller manages memory, e.g. windowed backfill):
            # keep the original all-at-once behaviour.
            with _timed_phase("Load target embedding clip rows"):
                embedding_clip_rows = _filter_embedding_clip_rows(
                    preloaded_embedding_clip_rows,
                    start_date=normalized_source_start,
                    end_date=normalized_source_end,
                )
                logger.info(
                    "Using %d preloaded target embedding clip row(s).",
                    len(embedding_clip_rows),
                )
            with _timed_phase("Score target embedding drift"):
                embedding_weekly = score_embedding_drift(
                    embedding_clip_rows,
                    config=embedding_config,
                    target_period_start=normalized_output_start,
                    target_period_end=normalized_output_end,
                )
            if include_embedding_control:
                with _timed_phase("Build all-species embedding scene control"):
                    scene_control = _build_embedding_scene_control(
                        pool,
                        min_confidence=embedding_min_confidence,
                        config=embedding_config,
                        min_control_species=embedding_min_control_species,
                        start_date=normalized_source_start,
                        end_date=normalized_source_end,
                        target_period_start=normalized_output_start,
                        target_period_end=normalized_output_end,
                        target_species_ids=set(
                            selected_species_ids or species_registry.birdweather_ids
                        ),
                        preloaded_clip_rows=preloaded_control_embedding_clip_rows,
                    )
            else:
                scene_control = pd.DataFrame()
            if include_embedding_clusters:
                with _timed_phase("Assign target embedding clusters"):
                    embedding_clusters = score_embedding_clusters(
                        embedding_clip_rows,
                        config=embedding_config,
                        target_period_start=normalized_output_start,
                        target_period_end=normalized_output_end,
                    )
            else:
                embedding_clusters = None
        with _timed_phase("Build modeling feature set"):
            features = build_species_week_feature_set(
                rows,
                target_species_ids=selected_species_ids,
                sovon_month_rows=sovon_rows,
                embedding_weekly_rows=embedding_weekly,
                min_observation_days=min_observation_days,
            )
            features = attach_embedding_control(features, scene_control)
        _log_weekly_frame_coverage(
            "modeling feature weeks to score",
            features,
            list_periods=True,
        )
        baseline_config = PopulationBaselineConfig(
            seasonal_window_weeks=seasonal_window_weeks,
            min_location_history=min_location_history,
            min_seasonal_history=min_seasonal_history,
            min_observation_days=min_observation_days,
            min_station_availability_ratio=min_station_availability_ratio,
            min_station_baseline_history=min_station_baseline_history,
            activity_components=tuple(population_activity_components),
        )
        logger.info(
            "Scoring population anomalies for %d feature row(s).", len(features)
        )
        with _timed_phase("Score population anomalies"):
            scored = score_population_anomalies(features, config=baseline_config)
            scored = _attach_score_context(scored, features)
        scorer = (
            score_behavior_anomalies_walk_forward
            if scoring_mode == "walk_forward"
            else score_behavior_anomalies
        )
        logger.info(
            "Scoring behavior anomalies in %s mode for %d row(s).",
            scoring_mode,
            len(scored),
        )
        with _timed_phase("Score behavior anomalies"):
            scored = scorer(
                scored,
                config=BehaviorAnomalyModelConfig(
                    min_model_rows=anomaly_min_model_rows,
                    contamination=anomaly_contamination,
                    random_state=anomaly_random_state,
                    amber_quantile=amber_quantile,
                    red_quantile=red_quantile,
                    include_environmental=include_environmental,
                    model_streams=tuple(model_streams),
                ),
            )
        _log_weekly_frame_coverage("scored modeling output", scored)
        persist_scored = _filter_period_window(
            scored,
            start_date=normalized_output_start,
            end_date=normalized_output_end,
        )
        persist_features = _filter_period_window(
            features,
            start_date=normalized_output_start,
            end_date=normalized_output_end,
        )
        persist_embedding_clusters = _filter_period_window(
            embedding_clusters,
            start_date=normalized_output_start,
            end_date=normalized_output_end,
        )
        if normalized_output_start is not None:
            _log_weekly_frame_coverage(
                "target scored modeling output to persist",
                persist_scored,
                list_periods=True,
            )
        with _timed_phase("Persist modeling outputs"):
            return _persist_outputs(
                persist_scored,
                source_week_rows=rows,
                features=persist_features,
                embedding_clip_rows=embedding_clip_rows,
                embedding_clusters=persist_embedding_clusters,
                skip_evaluation=skip_evaluation,
                rows_loaded=len(rows),
                pool=pool,
                parent_tuning_run_id=parent_tuning_run_id,
                run_id=run_id,
                status=run_status,
                replace_existing_run=replace_existing_run,
                replace_period_start=None if append_only else normalized_output_start,
                replace_period_end=None if append_only else normalized_output_end,
                config={
                    "seasonal_window_weeks": seasonal_window_weeks,
                    "min_location_history": min_location_history,
                    "min_seasonal_history": min_seasonal_history,
                    "min_observation_days": min_observation_days,
                    "target_species_only": target_species_only,
                    "species_ids": list(selected_species_ids)
                    if selected_species_ids
                    else None,
                    "include_embedding_drift": include_embedding_drift,
                    "include_embedding_clusters": include_embedding_clusters,
                    "include_embedding_control": include_embedding_control,
                    "embedding_min_confidence": embedding_min_confidence,
                    "embedding_min_baseline_clips": embedding_min_baseline_clips,
                    "embedding_min_week_clips": embedding_min_week_clips,
                    "embedding_k_neighbors": embedding_k_neighbors,
                    "embedding_cluster_min_samples": embedding_cluster_min_samples,
                    "embedding_cluster_distance_multiplier": embedding_cluster_distance_multiplier,
                    "embedding_cluster_distance_floor": embedding_cluster_distance_floor,
                    "embedding_cluster_distance_ceiling": embedding_cluster_distance_ceiling,
                    "anomaly_min_model_rows": anomaly_min_model_rows,
                    "anomaly_contamination": anomaly_contamination,
                    "anomaly_random_state": anomaly_random_state,
                    "amber_quantile": amber_quantile,
                    "red_quantile": red_quantile,
                    "include_environmental": include_environmental,
                    "model_streams": list(model_streams),
                    "scoring_mode": scoring_mode,
                    "include_sovon_context": include_sovon_context,
                    "skip_evaluation": skip_evaluation,
                    "source_start_date": (
                        normalized_source_start.isoformat()
                        if normalized_source_start
                        else None
                    ),
                    "source_end_date": (
                        normalized_source_end.isoformat()
                        if normalized_source_end
                        else None
                    ),
                    "output_period_start": (
                        normalized_output_start.isoformat()
                        if normalized_output_start
                        else None
                    ),
                    "output_period_end": (
                        normalized_output_end.isoformat()
                        if normalized_output_end
                        else None
                    ),
                },
            )
    finally:
        logger.info(
            "Model anomaly scoring finished in %.2fs.",
            perf_counter() - run_started,
        )
        if own_pool:
            pool.close()


def _load_species_week_rows(
    pool: ConnectionPool,
    *,
    target_species_only: bool,
    species_ids: tuple[str, ...] | list[str] | None = None,
    period_start: date | None = None,
    period_end: date | None = None,
) -> pd.DataFrame:
    filters = [
        "row_kind IN ('species', 'community')",
        "period_grain = 'week'",
        "location_type = 'grid'",
    ]
    params: list[Any] = []
    selected_species_ids = _effective_target_species_ids(
        target_species_only=target_species_only,
        species_ids=species_ids,
    )
    if selected_species_ids:
        filters.append("(row_kind = 'community' OR species_id = ANY(%s))")
        params.append(list(selected_species_ids))
    if period_start is not None:
        filters.append("period_start >= %s")
        params.append(period_start)
    if period_end is not None:
        filters.append("period_start <= %s")
        params.append(period_end)

    query = psycopg2.sql.SQL(
        """
        SELECT
            species_tracker_id,
            row_kind,
            species_id,
            location_type,
            location_id,
            station_id,
            species_tracker_tbl.grid_cell_id,
            gc.lat AS grid_centroid_lat,
            gc.lon AS grid_centroid_lon,
            species_tracker_tbl.grid_system AS grid_system,
            period_start,
            period_end,
            period_day_count,
            total_detections,
            active_days,
            active_station_count,
            species_station_count,
            detections_per_active_day,
            temporal_occupancy_pct,
            relative_abundance_pct,
            station_presence_pct,
            species_richness,
            target_species_richness,
            target_species_share_pct,
            confidence_mean,
            confidence_p10,
            confidence_p50,
            confidence_p90,
            high_confidence_pct,
            processed_clip_pct,
            embedding_count,
            station_lifetime_availability_pct,
            station_availability_90d_pct,
            station_days_since_last,
            station_data_quality_grade,
            knmi_temp_mean_c,
            knmi_temp_max_c,
            knmi_precip_mm,
            knmi_humidity_mean_pct,
            knmi_wind_max_ms,
            gee_ndvi_mean,
            gee_soil_moisture_mean,
            cds_spi_1m_mean,
            cds_spi_3m_mean,
            cds_spei_3m_mean,
            cds_spei_12m_mean,
            background_top_label,
            background_top_share_pct
        FROM species_tracker_tbl
        LEFT JOIN grid_cells_tbl gc ON gc.grid_cell_id = species_tracker_tbl.grid_cell_id
        WHERE {}
        ORDER BY species_id, location_type, location_id, period_start
    """
    ).format(_sql_where_clause(filters))
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        records = cur.fetchall()

    frame = pd.DataFrame.from_records(records)
    logger.info("Loaded %d weekly species-tracker row(s).", len(frame))
    return frame


def _load_sovon_month_rows(
    pool: ConnectionPool,
    *,
    target_species_only: bool,
    species_ids: tuple[str, ...] | list[str] | None = None,
) -> pd.DataFrame:
    filters = ["year IS NOT NULL", "month IS NOT NULL"]
    params: list[Any] = []
    selected_species_ids = _effective_target_species_ids(
        target_species_only=target_species_only,
        species_ids=species_ids,
    )
    if selected_species_ids:
        sovon_ids, english_names = _sovon_filter_values_for_species_ids(
            selected_species_ids
        )
        if not sovon_ids and not english_names:
            logger.info("Loaded 0 SOVON mortality context row(s).")
            return pd.DataFrame()
        filters.append("(sovon_species_id = ANY(%s) OR species_english = ANY(%s))")
        params.extend([sovon_ids, english_names])

    query = psycopg2.sql.SQL(
        """
        SELECT
            mortality_id,
            observation_date,
            year,
            month,
            species_english,
            species_dutch,
            sovon_species_id,
            dead_bird_count,
            n_total
        FROM sovon_tbl
        WHERE {}
        ORDER BY species_english, year, month
    """
    ).format(_sql_where_clause(filters))
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        records = cur.fetchall()

    frame = pd.DataFrame.from_records(records)
    logger.info("Loaded %d SOVON mortality context row(s).", len(frame))
    return frame


def _load_embedding_clip_rows(
    pool: ConnectionPool,
    *,
    target_species_only: bool,
    species_ids: tuple[str, ...] | list[str] | None = None,
    min_confidence: float,
    start_date: date | None = None,
    end_date: date | None = None,
    apply_species_threshold_floor: bool = True,
    show_progress: bool = True,
    station_ids: list[str] | None = None,
    parse_embeddings: bool = False,
) -> pd.DataFrame:
    # NOTE: This query is unbounded in time and (when target_species_only is
    # False) in species, so it can return very large result sets on a mature
    # database. Callers that load all species (e.g. the scene-control pass)
    # should expect a heavier load; bound it via configuration / retention if
    # the clip table grows large.
    query_min_confidence = min_confidence
    selected_species_ids = _effective_target_species_ids(
        target_species_only=target_species_only,
        species_ids=species_ids,
    )
    if selected_species_ids and apply_species_threshold_floor:
        minimum_species_threshold = species_registry.minimum_recommended_threshold
        if minimum_species_threshold is not None:
            query_min_confidence = min(query_min_confidence, minimum_species_threshold)
    filters = [
        "b.has_processed_clip",
        "(b.perch_v2_embedding IS NOT NULL OR b.birdnet_embedding IS NOT NULL)",
        "b.confidence >= %s",
    ]
    params: list[Any] = [query_min_confidence]
    if selected_species_ids:
        filters.append("b.species_id = ANY(%s)")
        params.append(list(selected_species_ids))
    if station_ids is not None:
        filters.append("b.station_id = ANY(%s)")
        params.append(list(station_ids))
    if start_date is not None:
        filters.append("b.detected_at >= %s::date")
        filters.append("b.observation_date >= %s")
        params.extend([start_date, start_date])
    if end_date is not None:
        filters.append("b.detected_at < (%s::date + INTERVAL '1 day')")
        filters.append("b.observation_date <= %s")
        params.extend([end_date, end_date])

    query = psycopg2.sql.SQL(
        """
        WITH candidate_detections AS (
            SELECT
                b.detection_id,
                b.detected_at,
                b.observation_date,
                b.species_id,
                b.station_id,
                b.confidence,
                COALESCE(b.perch_v2_embedding::text, b.birdnet_embedding::text)
                    AS embedding_vector,
                CASE
                    WHEN b.perch_v2_embedding IS NOT NULL THEN 'perch_v2'
                    WHEN b.birdnet_embedding IS NOT NULL THEN 'birdnet'
                    ELSE 'unknown'
                END AS embedding_model,
                b.feature_artifacts->>'processed_clip' AS processed_clip_path,
                b.background_sound_clf
            FROM birdweather_tbl b
            WHERE {}
        )
        SELECT
            c.detection_id,
            c.detected_at,
            c.observation_date,
            c.species_id,
            c.station_id,
            fn_point_to_cell(s.lat, s.lon, 'nl_geodesic_5km') AS grid_cell_id,
            c.confidence,
            c.embedding_vector,
            c.embedding_model,
            c.processed_clip_path,
            c.background_sound_clf
        FROM candidate_detections c
        JOIN birdweather_stations_tbl s
          ON s.station_id = c.station_id
         AND s.lat IS NOT NULL
         AND s.lon IS NOT NULL
    """
    ).format(_sql_where_clause(filters))
    count_query = psycopg2.sql.SQL(
        """
        SELECT COUNT(*) AS total_rows
        FROM birdweather_tbl b
        JOIN birdweather_stations_tbl s
          ON s.station_id = b.station_id
         AND s.lat IS NOT NULL
         AND s.lon IS NOT NULL
        WHERE {}
    """
    ).format(_sql_where_clause(filters))
    records: list[Any] = []
    with pool.connection() as conn:
        try:
            # Force a serial plan for this load. The query carries large embedding
            # vectors; Postgres parallel workers allocate dynamic shared memory in
            # /dev/shm, which is only 64 MB in the default container, so a parallel
            # plan can die with "could not resize shared memory segment ... No
            # space left on device". SET LOCAL keeps the change scoped to this load.
            with conn.cursor() as setup_cur:
                setup_cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
            total_rows = 0
            if show_progress:
                with conn.cursor(
                    cursor_factory=psycopg2.extras.RealDictCursor
                ) as count_cur:
                    count_cur.execute(count_query, params)
                    count_row = count_cur.fetchone() or {}
                    total_rows = int(count_row.get("total_rows") or 0)
                logger.info(
                    "BirdWeather embedding clip preload will load %d row(s).",
                    total_rows,
                )
            with conn.cursor(
                name="embedding_clip_rows_cursor",
                cursor_factory=psycopg2.extras.RealDictCursor,
            ) as cur:
                cur.itersize = _EMBEDDING_FETCH_BATCH_SIZE
                cur.execute(query, params)
                progress = (
                    tqdm(
                        total=total_rows,
                        desc="Load BirdWeather embedding clips",
                        unit="rows",
                    )
                    if show_progress
                    else None
                )
                try:
                    while True:
                        batch = cur.fetchmany(_EMBEDDING_FETCH_BATCH_SIZE)
                        if not batch:
                            break
                        if parse_embeddings:
                            # Parse + normalise each embedding now and drop the
                            # multi-KB text immediately, so the raw text and the
                            # float64 array never coexist. _normalise_input then
                            # reuses embedding_array instead of re-parsing,
                            # avoiding a second full copy of the vectors.
                            for row in batch:
                                record = dict(row)
                                record["embedding_array"] = (
                                    parse_embedding_vector(
                                        record.pop("embedding_vector", None)
                                    )
                                )
                                records.append(record)
                        else:
                            records.extend(batch)
                        if progress is not None:
                            progress.update(len(batch))
                finally:
                    if progress is not None:
                        progress.close()
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    frame = pd.DataFrame.from_records(records)
    logger.info("Loaded %d BirdWeather embedding clip row(s).", len(frame))
    return frame


def _filter_embedding_clip_rows(
    rows: pd.DataFrame,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> pd.DataFrame:
    """Filter preloaded embedding rows with the same date rules as SQL loading."""
    if rows.empty or (start_date is None and end_date is None):
        return rows.copy()

    detected_at = pd.to_datetime(rows["detected_at"], utc=True, errors="coerce")
    observation_date = pd.to_datetime(
        rows["observation_date"],
        errors="coerce",
    ).dt.date
    mask = detected_at.notna() & observation_date.notna()
    if start_date is not None:
        start_ts = pd.Timestamp(start_date).tz_localize(UTC)
        mask &= detected_at >= start_ts
        mask &= observation_date >= start_date
    if end_date is not None:
        end_ts = (pd.Timestamp(end_date) + pd.Timedelta(days=1)).tz_localize(UTC)
        mask &= detected_at < end_ts
        mask &= observation_date <= end_date
    return rows.loc[mask].copy()


def _build_embedding_scene_control(
    pool: ConnectionPool,
    *,
    min_confidence: float,
    config: EmbeddingDriftConfig,
    min_control_species: int,
    target_species_ids: set[str],
    start_date: date | None = None,
    end_date: date | None = None,
    target_period_start: date | None = None,
    target_period_end: date | None = None,
    preloaded_clip_rows: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Score all-species clips and derive co-located non-target scene drift.

    Loads clips for every species (not just targets), scores their weekly
    embedding drift, then aggregates the non-target community to a per
    location-week control. The target species' own drift is excluded so the
    control reflects only scene-wide change in the surrounding soundscape.
    """
    if preloaded_clip_rows is None:
        control_clip_rows = _load_embedding_clip_rows(
            pool,
            target_species_only=False,
            min_confidence=min_confidence,
            start_date=start_date,
            end_date=end_date,
        )
    else:
        control_clip_rows = _filter_embedding_clip_rows(
            preloaded_clip_rows,
            start_date=start_date,
            end_date=end_date,
        )
        logger.info(
            "Using %d preloaded all-species embedding clip row(s).",
            len(control_clip_rows),
        )
    if control_clip_rows.empty:
        return pd.DataFrame()
    control_weekly = score_embedding_drift(
        control_clip_rows,
        config=config,
        target_period_start=target_period_start,
        target_period_end=target_period_end,
    )
    return score_embedding_scene_control(
        control_weekly,
        target_species_ids=target_species_ids,
        min_control_species=min_control_species,
    )


def _distinct_embedding_species_ids(
    pool: ConnectionPool,
    *,
    min_confidence: float,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[str]:
    """Distinct BirdWeather species ids that have usable embedding clips.

    Uses the same filters as the all-species clip load (no species-threshold
    floor), so the streamed scene-control pass sees exactly the same population
    of species as the original monolithic all-species load.
    """
    filters = [
        "b.has_processed_clip",
        "(b.perch_v2_embedding IS NOT NULL OR b.birdnet_embedding IS NOT NULL)",
        "b.confidence >= %s",
    ]
    params: list[Any] = [min_confidence]
    if start_date is not None:
        filters.append("b.detected_at >= %s::date")
        filters.append("b.observation_date >= %s")
        params.extend([start_date, start_date])
    if end_date is not None:
        filters.append("b.detected_at < (%s::date + INTERVAL '1 day')")
        filters.append("b.observation_date <= %s")
        params.extend([end_date, end_date])
    query = psycopg2.sql.SQL(
        """
        SELECT DISTINCT b.species_id
        FROM birdweather_tbl b
        JOIN birdweather_stations_tbl s
          ON s.station_id = b.station_id
         AND s.lat IS NOT NULL
         AND s.lon IS NOT NULL
        WHERE {}
        """
    ).format(_sql_where_clause(filters))
    species: list[str] = []
    with pool.connection() as conn:
        # Force a serial plan. This DISTINCT scans the full birdweather_tbl
        # hypertable; a parallel plan allocates dynamic shared memory in
        # /dev/shm (only 64 MB in the default container), so it can die with
        # "could not resize shared memory segment ... No space left on device".
        # Mirrors the guard already used in _load_embedding_clip_rows.
        with conn.cursor() as setup_cur:
            setup_cur.execute("SET LOCAL max_parallel_workers_per_gather = 0")
        with conn.cursor() as cur:
            cur.execute(query, params)
            for row in cur.fetchall():
                if row and row[0] is not None:
                    species.append(str(row[0]))
        conn.commit()
    return species


def _species_embedding_threshold(
    species_id: str, default_min_confidence: float
) -> float:
    """Confidence threshold that ``_normalise_input`` applies to a species.

    Loading each species at exactly this threshold (instead of the global
    floor) yields an identical scored set, because ``_normalise_input`` drops
    every clip below the species' recommended threshold anyway. It simply
    avoids fetching + parsing clips that would be discarded, which sharply
    lowers peak memory for the busiest species.
    """
    threshold = species_registry.recommended_threshold_by_birdweather_id.get(
        str(species_id)
    )
    if threshold is None:
        return float(default_min_confidence)
    return float(threshold)


def _compute_embedding_stage_streamed(
    pool: ConnectionPool,
    *,
    selected_species_ids: tuple[str, ...] | list[str] | None,
    config: EmbeddingDriftConfig,
    embedding_min_confidence: float,
    include_embedding_clusters: bool,
    include_embedding_control: bool,
    embedding_min_control_species: int,
    source_start: date | None,
    source_end: date | None,
    output_start: date | None,
    output_end: date | None,
) -> tuple[pd.DataFrame, pd.DataFrame | None, pd.DataFrame, pd.DataFrame]:
    """Compute embedding drift, clusters and scene control one species at a time.

    This is the memory-safe equivalent of the monolithic embedding stage: each
    species' clips are loaded, scored and freed before moving on, so peak memory
    is bounded by a single species rather than the whole clip table. Results are
    identical to loading every clip at once because ``score_embedding_drift`` and
    ``score_embedding_clusters`` group within species and the embedding baseline
    is built within species (same week-of-year window), so concatenating the
    per-species outputs reproduces the all-at-once output exactly.

    Returns ``(embedding_weekly, embedding_clusters, scene_control,
    coverage_frame)`` where ``coverage_frame`` holds only the target clips'
    ``detected_at`` column, which is all the source-coverage summary needs.
    """
    target_ids = [
        str(value)
        for value in (selected_species_ids or species_registry.birdweather_ids)
    ]
    target_id_set = set(target_ids)

    drift_parts: list[pd.DataFrame] = []
    cluster_parts: list[pd.DataFrame] = []
    detected_at_parts: list[pd.DataFrame] = []

    # --- target species: drift (+ clusters), one species at a time ---
    # Per-species is the finest grain that is still exact: the drift baseline
    # cascade (_candidate_baselines) can fall back to a *network* baseline that
    # spans all of a species' grid cells, so cells cannot be split. To keep peak
    # memory bounded we (1) load each species only at its own recommended
    # confidence threshold -- the exact set _normalise_input keeps -- and
    # (2) parse+normalise embeddings inside the loader so the raw text and the
    # float64 array never coexist. Both are result-preserving; together they hold
    # roughly one float64 copy of a single species' scored clips.
    for species_id in target_ids:
        species_threshold = _species_embedding_threshold(
            species_id, embedding_min_confidence
        )
        clips = _load_embedding_clip_rows(
            pool,
            target_species_only=False,
            species_ids=[species_id],
            min_confidence=species_threshold,
            apply_species_threshold_floor=False,
            start_date=source_start,
            end_date=source_end,
            show_progress=False,
            parse_embeddings=True,
        )
        if clips.empty:
            del clips
            continue
        logger.info(
            "Streamed target species %s: %d embedding clip row(s).",
            species_id,
            len(clips),
        )
        if "detected_at" in clips.columns:
            detected_at_parts.append(clips[["detected_at"]].copy())
        species_drift = score_embedding_drift(
            clips,
            config=config,
            target_period_start=output_start,
            target_period_end=output_end,
        )
        if not species_drift.empty:
            drift_parts.append(species_drift)
        if include_embedding_clusters:
            species_clusters = score_embedding_clusters(
                clips,
                config=config,
                target_period_start=output_start,
                target_period_end=output_end,
            )
            if not species_clusters.empty:
                cluster_parts.append(species_clusters)
        del clips, species_drift
        gc.collect()

    embedding_weekly = (
        pd.concat(drift_parts, ignore_index=True) if drift_parts else pd.DataFrame()
    )
    embedding_clusters = (
        pd.concat(cluster_parts, ignore_index=True) if cluster_parts else None
    )
    coverage_frame = (
        pd.concat(detected_at_parts, ignore_index=True)
        if detected_at_parts
        else pd.DataFrame()
    )

    # --- scene control: non-target community drift, one species at a time ---
    scene_control = pd.DataFrame()
    if include_embedding_control:
        control_species = _distinct_embedding_species_ids(
            pool,
            min_confidence=embedding_min_confidence,
            start_date=source_start,
            end_date=source_end,
        )
        # Target species are excluded from the control aggregation anyway, so we
        # skip loading them here: identical result, and it avoids re-loading the
        # heavy target species a second time.
        control_species = [s for s in control_species if s not in target_id_set]
        control_parts: list[pd.DataFrame] = []
        for species_id in control_species:
            clips = _load_embedding_clip_rows(
                pool,
                target_species_only=False,
                species_ids=[species_id],
                min_confidence=embedding_min_confidence,
                start_date=source_start,
                end_date=source_end,
                apply_species_threshold_floor=False,
                show_progress=False,
            )
            if clips.empty:
                del clips
                continue
            species_drift = score_embedding_drift(
                clips,
                config=config,
                target_period_start=output_start,
                target_period_end=output_end,
            )
            if not species_drift.empty:
                control_parts.append(species_drift)
            del clips, species_drift
            gc.collect()
        if control_parts:
            control_weekly = pd.concat(control_parts, ignore_index=True)
            scene_control = score_embedding_scene_control(
                control_weekly,
                target_species_ids=target_id_set,
                min_control_species=embedding_min_control_species,
            )

    return embedding_weekly, embedding_clusters, scene_control, coverage_frame


def _coerce_date(value: date | str | None) -> date | None:
    if value is None or isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _validate_optional_window(
    start_date: date | None,
    end_date: date | None,
    *,
    label: str,
) -> None:
    if (start_date is None) != (end_date is None):
        msg = f"{label}_start_date and {label}_end_date must be provided together"
        raise ValueError(msg)
    if start_date is not None and end_date is not None and end_date < start_date:
        msg = f"{label} end date ({end_date}) must be >= start date ({start_date})"
        raise ValueError(msg)


def _filter_period_window(
    frame: pd.DataFrame | None,
    *,
    start_date: date | None,
    end_date: date | None,
) -> pd.DataFrame | None:
    if frame is None or start_date is None or end_date is None or frame.empty:
        return frame
    if "period_start" not in frame.columns:
        return frame
    result = frame.copy()
    periods = pd.to_datetime(result["period_start"], errors="coerce").dt.normalize()
    start_ts = pd.Timestamp(start_date)
    end_ts = pd.Timestamp(end_date)
    mask = periods.notna() & (periods >= start_ts) & (periods <= end_ts)
    return result.loc[mask].reset_index(drop=True)


def _sql_where_clause(filters: list[str]) -> Any:
    return psycopg2.sql.SQL(" AND ").join(
        psycopg2.sql.SQL(filter_clause) for filter_clause in filters
    )


def _attach_score_context(scored: pd.DataFrame, features: pd.DataFrame) -> pd.DataFrame:
    if scored.empty or features.empty:
        return scored

    keys = ["species_id", "location_type", "location_id", "period_start"]
    context_columns = [
        "feature_set_version",
        "grid_centroid_lat",
        "grid_centroid_lon",
        "year",
        "week_of_year",
        "month_start",
        "species_english",
        "sovon_species_id",
        "sovon_species_english",
        "sovon_dead_bird_count",
        "sovon_n_total",
        "sovon_mortality_rate",
        "sovon_expected_dead_bird_count",
        "sovon_mortality_z",
        "sovon_mortality_score",
        "sovon_mortality_top_decile_flag",
        "sovon_lag_0_to_3mo_max_score",
        "sovon_lag_0_to_3mo_dead_birds",
        "sovon_has_context",
        "embedding_drift_score",
        "embedding_drift_control_score",
        "embedding_control_species_count",
        "embedding_control_available",
        "embedding_outlier_share",
        "embedding_clip_count_scored",
        "embedding_baseline_size",
        "embedding_baseline_level",
        "embedding_backend_clip_counts",
        "embedding_backend_scored_clip_counts",
        "embedding_primary_backend",
        "top_detection_ids",
        "top_audio_artifact_paths",
        "background_noise_summary",
        "audio_quality_flag",
        *ENVIRONMENTAL_CONTEXT_COLUMNS,
    ]
    available_columns = [column for column in context_columns if column in features]

    left = scored.copy()
    right = features[[*keys, *available_columns]].copy()
    left["period_start"] = pd.to_datetime(left["period_start"]).dt.strftime("%Y-%m-%d")
    right["period_start"] = pd.to_datetime(right["period_start"]).dt.strftime(
        "%Y-%m-%d"
    )
    if "month_start" in right.columns:
        right["month_start"] = pd.to_datetime(right["month_start"]).dt.strftime(
            "%Y-%m-%d"
        )
    return left.merge(
        right.drop_duplicates(keys),
        on=keys,
        how="left",
    )


def _persist_outputs(
    scored: pd.DataFrame,
    *,
    source_week_rows: pd.DataFrame | None = None,
    features: pd.DataFrame | None = None,
    embedding_clip_rows: pd.DataFrame | None = None,
    embedding_clusters: pd.DataFrame | None = None,
    skip_evaluation: bool = False,
    rows_loaded: int,
    pool: ConnectionPool,
    parent_tuning_run_id: str | None = None,
    run_id: str | None = None,
    run_kind: str = "anomaly_scoring",
    status: str = "complete",
    replace_existing_run: bool = True,
    replace_period_start: date | None = None,
    replace_period_end: date | None = None,
    config: dict[str, object],
) -> ModelingRunStats:
    generated_at = datetime.now(UTC)
    run_id = run_id or generated_at.strftime("%Y%m%dT%H%M%SZ")
    feature_rows = len(features) if features is not None else 0
    cluster_rows = 0
    cluster_exemplar_rows = 0
    if embedding_clusters is not None:
        cluster_rows = len(embedding_clusters)
        cluster_exemplar_rows = int(
            embedding_clusters.get("is_cluster_exemplar", pd.Series(dtype=bool))
            .fillna(False)
            .astype(bool)
            .sum()
        )

    stats = ModelingRunStats(
        run_id=run_id,
        database_tables=(
            "modeling_runs_tbl, modeling_species_week_features_tbl, "
            "modeling_species_week_scores_tbl, modeling_embedding_clusters_tbl"
        ),
        rows_loaded=rows_loaded,
        feature_rows=feature_rows,
        rows_scored=len(scored),
        alerts_green=_alert_count(scored, "green"),
        alerts_amber=_alert_count(scored, "amber"),
        alerts_red=_alert_count(scored, "red"),
        sparse_baseline_rows=_sparse_baseline_count(scored),
        cluster_rows=cluster_rows,
        cluster_exemplar_rows=cluster_exemplar_rows,
    )
    source_coverage = _build_source_coverage_summary(
        source_week_rows=source_week_rows,
        features=features,
        scored=scored,
        embedding_clip_rows=embedding_clip_rows,
    )
    if skip_evaluation:
        logger.info("Scores-only mode enabled; no validation report will be built.")
        validation_report = None
    else:
        with _timed_phase("Build modeling validation report"):
            validation_report = {
                "run_id": run_id,
                "generated_at": generated_at.isoformat(),
                "config": config,
                "validation": build_validation_report(scored, top_k=25),
            }
    with _timed_phase("Write modeling rows to Postgres"):
        persist_modeling_run(
            run_id=run_id,
            run_kind=run_kind,
            status=status,
            generated_at=generated_at,
            completed_at=datetime.now(UTC) if status == "complete" else None,
            parent_tuning_run_id=parent_tuning_run_id,
            config=config,
            stats=stats.to_metadata(),
            source_coverage=source_coverage,
            validation_report=validation_report,
            features=features,
            scores=scored,
            clusters=embedding_clusters,
            pool=pool,
            replace_existing_run=replace_existing_run,
            replace_period_start=replace_period_start,
            replace_period_end=replace_period_end,
        )
    logger.info(
        "Persisted %d modeling score row(s) to Postgres run %s.",
        len(scored),
        run_id,
    )
    return stats


def _log_weekly_frame_coverage(
    label: str,
    frame: pd.DataFrame | None,
    *,
    list_periods: bool = False,
) -> None:
    if frame is None or frame.empty:
        logger.info("%s: no weekly rows.", label)
        return
    if "period_start" not in frame.columns:
        logger.info("%s: %d row(s), no period_start column.", label, len(frame))
        return

    period_start = pd.to_datetime(frame["period_start"], errors="coerce").dropna()
    if period_start.empty:
        logger.info("%s: %d row(s), no valid period_start values.", label, len(frame))
        return

    periods = sorted({value.date().isoformat() for value in period_start})
    period_end_min, period_end_max = (None, None)
    if "period_end" in frame.columns:
        period_end = pd.to_datetime(frame["period_end"], errors="coerce").dropna()
        if not period_end.empty:
            period_end_min = period_end.min().date().isoformat()
            period_end_max = period_end.max().date().isoformat()

    logger.info(
        "%s: rows=%d weekly_periods=%d period_start=%s..%s period_end=%s..%s "
        "species=%s locations=%s.",
        label,
        len(frame),
        len(periods),
        periods[0],
        periods[-1],
        period_end_min,
        period_end_max,
        _unique_count(frame, "species_id"),
        _unique_count(frame, "location_id"),
    )
    if list_periods:
        _log_period_chunks(label, periods)


def _log_period_chunks(label: str, periods: list[str], *, chunk_size: int = 16) -> None:
    for offset in range(0, len(periods), chunk_size):
        chunk = periods[offset : offset + chunk_size]
        logger.info(
            "%s period_start %d-%d/%d: %s",
            label,
            offset + 1,
            offset + len(chunk),
            len(periods),
            ", ".join(chunk),
        )


def _unique_count(frame: pd.DataFrame, column: str) -> int | str:
    if column not in frame.columns:
        return "n/a"
    return int(frame[column].dropna().nunique())


def _build_source_coverage_summary(
    *,
    source_week_rows: pd.DataFrame | None,
    features: pd.DataFrame | None,
    scored: pd.DataFrame,
    embedding_clip_rows: pd.DataFrame | None,
) -> dict[str, dict[str, int | str | None]]:
    return {
        "species_tracker": _frame_coverage_summary(
            source_week_rows,
            start_column="period_start",
            end_column="period_end",
        ),
        "features": _frame_coverage_summary(
            features,
            start_column="period_start",
            end_column="period_end",
        ),
        "scored": _frame_coverage_summary(
            scored,
            start_column="period_start",
            end_column="period_end",
        ),
        "embedding_clips": _frame_coverage_summary(
            embedding_clip_rows,
            start_column="detected_at",
            end_column="detected_at",
        ),
    }


def _frame_coverage_summary(
    frame: pd.DataFrame | None,
    *,
    start_column: str,
    end_column: str,
) -> dict[str, int | str | None]:
    start_min, start_max = _frame_date_bounds(frame, start_column)
    end_min, end_max = _frame_date_bounds(frame, end_column)
    return {
        "row_count": 0 if frame is None else len(frame),
        f"{start_column}_min": start_min,
        f"{start_column}_max": start_max,
        f"{end_column}_min": end_min,
        f"{end_column}_max": end_max,
    }


def _frame_date_bounds(
    frame: pd.DataFrame | None,
    column: str,
) -> tuple[str | None, str | None]:
    if frame is None or frame.empty or column not in frame.columns:
        return None, None

    series = pd.to_datetime(frame[column], errors="coerce", utc=True).dropna()
    if series.empty:
        return None, None

    return (
        series.min().date().isoformat(),
        series.max().date().isoformat(),
    )


def _alert_count(frame: pd.DataFrame, level: str) -> int:
    if frame.empty or "alert_level" not in frame.columns:
        return 0
    return int((frame["alert_level"] == level).sum())


def _sparse_baseline_count(frame: pd.DataFrame) -> int:
    if frame.empty or "quality_mask_reason" not in frame.columns:
        return 0
    return int(frame["quality_mask_reason"].str.contains("sparse_baseline").sum())
