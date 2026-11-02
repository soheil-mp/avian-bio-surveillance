"""Operational CLI for running a full persisted modeling backfill."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from time import perf_counter

import pandas as pd
import psycopg2.extras

from avian_biosurveillance.db.database import ConnectionPool
from avian_biosurveillance.db.loaders.modeling import persist_modeling_run
from avian_biosurveillance.modeling.pipeline import (
    _load_embedding_clip_rows,
    run_model_anomaly_scoring,
)
from avian_biosurveillance.modeling.schemas import ModelingRunStats
from avian_biosurveillance.modeling.settings import SETTINGS
from avian_biosurveillance.orchestration.derived_aggregates_flow import (
    refresh_species_dim_task,
    refresh_species_tracker_task,
)


logger = logging.getLogger(__name__)
_DEFAULT_REFRESH_START_DATE = date(2024, 1, 1)
_DEFAULT_REFRESH_END_DATE = date(2025, 12, 31)
_DEFAULT_WINDOW_DAYS = 7
_DEFAULT_SCORING_BATCH_WINDOWS = 8
_DEFAULT_DERIVED_REFRESH_CHUNK_WINDOWS = 8
_WINDOW_MARKER_RUN_KIND = "anomaly_scoring_window"


@dataclass(frozen=True, slots=True)
class SpeciesTrackerCoverage:
    """Weekly species-tracker coverage used by modeling."""

    min_period_start: date | None
    max_period_start: date | None
    weekly_periods: int
    rows: int

    def to_log_payload(self) -> dict[str, int | str | None]:
        """Return JSON-serializable coverage values."""
        return {
            "min_period_start": (
                self.min_period_start.isoformat() if self.min_period_start else None
            ),
            "max_period_start": (
                self.max_period_start.isoformat() if self.max_period_start else None
            ),
            "weekly_periods": self.weekly_periods,
            "rows": self.rows,
        }


def _parse_date(value: str) -> date:
    """Parse an ISO date argument."""
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        msg = f"{value!r} is not a valid ISO date (YYYY-MM-DD)"
        raise argparse.ArgumentTypeError(msg) from exc


def _week_start(value: date) -> date:
    """Return the Monday period_start for a date."""
    return value - timedelta(days=value.weekday())


def _normalise_species_ids(species_ids: Sequence[str] | None) -> tuple[str, ...] | None:
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


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run the full weekly model anomaly scorer and persist outputs to Postgres."
        )
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"],
        help="Logging level for progress output.",
    )
    parser.add_argument(
        "--scoring-mode",
        choices=["retrospective", "walk_forward"],
        default="retrospective",
        help="Behavior anomaly scoring mode.",
    )
    parser.add_argument(
        "--preset",
        choices=["fusion", "bioacoustic", "population"],
        default="fusion",
        help=(
            "Model feature preset. fusion uses population + bioacoustic streams; "
            "bioacoustic uses only embedding drift/outlier features; population "
            "uses only count/population features."
        ),
    )
    parser.add_argument(
        "--include-environmental",
        action="store_true",
        help=(
            "Add curated environmental context features to the IsolationForest. "
            "Mortality labels remain evaluation-only and are not model inputs."
        ),
    )
    parser.add_argument(
        "--all-species",
        action="store_true",
        help="Score all species instead of the configured target species set.",
    )
    parser.add_argument(
        "--species-id",
        action="append",
        dest="species_ids",
        help=(
            "Restrict modeling to one BirdWeather species ID. Can be repeated, "
            "for example --species-id 35 --species-id 61."
        ),
    )
    parser.add_argument(
        "--no-embedding-drift",
        action="store_true",
        help="Skip embedding drift and cluster scoring.",
    )
    parser.add_argument(
        "--no-embedding-clusters",
        action="store_true",
        help="Skip detection-level embedding cluster assignment while keeping drift scores.",
    )
    parser.add_argument(
        "--no-embedding-control",
        action="store_true",
        help="Skip all-species acoustic scene-control drift while keeping target drift scores.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help=(
            "Fast historical backfill mode: score all pending windows in one pass "
            "and skip embedding-vector drift, embedding clusters, and all-species "
            "scene-control drift."
        ),
    )
    parser.add_argument(
        "--no-sovon-context",
        action="store_true",
        help="Skip SOVON mortality context attachment for this modeling run.",
    )
    parser.add_argument(
        "--no-evaluation",
        action="store_true",
        help="Skip validation/evaluation report generation and persist scores only.",
    )
    parser.add_argument(
        "--refresh-derived",
        action="store_true",
        help=(
            "Refresh species_dim and the modeling slice of species_tracker_tbl "
            "before running modeling."
        ),
    )
    parser.add_argument(
        "--windowed",
        action="store_true",
        help=(
            "Run modeling one window at a time, refreshing derived tables and "
            "skipping previously completed windows."
        ),
    )
    parser.add_argument(
        "--run-id",
        help=(
            "Stable aggregate run id for a windowed backfill. Defaults to a "
            "deterministic id from the window and scoring options."
        ),
    )
    parser.add_argument(
        "--start-date",
        "--refresh-start-date",
        dest="refresh_start_date",
        type=_parse_date,
        default=_DEFAULT_REFRESH_START_DATE,
        help="Start date for the pre-modeling species_tracker refresh.",
    )
    parser.add_argument(
        "--end-date",
        "--refresh-end-date",
        dest="refresh_end_date",
        type=_parse_date,
        default=_DEFAULT_REFRESH_END_DATE,
        help="End date for the pre-modeling species_tracker refresh.",
    )
    parser.add_argument(
        "--refresh-scope",
        choices=["modeling", "full"],
        default="modeling",
        help="species_tracker refresh scope used with --refresh-derived.",
    )
    parser.add_argument(
        "--derived-refresh-mode",
        choices=["range", "window", "none"],
        default="range",
        help=(
            "In --windowed mode, refresh derived tables once for the full range, "
            "once per window, or not at all."
        ),
    )
    parser.add_argument(
        "--window-days",
        type=int,
        default=_DEFAULT_WINDOW_DAYS,
        help="Number of days per modeling backfill window.",
    )
    parser.add_argument(
        "--scoring-batch-windows",
        type=int,
        default=_DEFAULT_SCORING_BATCH_WINDOWS,
        help=(
            "Number of pending windows to score in one modeling pass. Use 1 for "
            "strict per-window scoring or 0 to score all pending windows at once."
        ),
    )
    parser.add_argument(
        "--history-start-date",
        type=_parse_date,
        help=(
            "Optional lower bound for modeling history loaded into each window. "
            "Defaults to --refresh-start-date."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Recompute windows even if they already have completed output.",
    )
    parser.add_argument(
        "--no-derived-refresh",
        action="store_true",
        help="In --windowed mode, do not refresh species_dim/species_tracker_tbl.",
    )
    return parser.parse_args(argv)


def _model_streams_for_preset(preset: str) -> tuple[str, ...]:
    if preset == "bioacoustic":
        return ("bioacoustic",)
    if preset == "population":
        return ("population",)
    return ("population", "bioacoustic")


def _modeling_options(args: argparse.Namespace) -> dict[str, object]:
    species_ids = _normalise_species_ids(args.species_ids)
    if args.all_species and species_ids:
        msg = "--all-species cannot be combined with --species-id."
        raise ValueError(msg)
    include_embedding_drift = (
        args.preset != "population" and not args.fast and not args.no_embedding_drift
    )
    if args.preset == "bioacoustic" and not include_embedding_drift:
        msg = "--preset bioacoustic requires embedding drift; remove --fast/--no-embedding-drift."
        raise ValueError(msg)

    model_streams = tuple(
        stream
        for stream in _model_streams_for_preset(args.preset)
        if stream != "bioacoustic" or include_embedding_drift
    )
    if not model_streams:
        msg = "Selected modeling options leave no usable model stream."
        raise ValueError(msg)

    include_embedding_clusters = (
        include_embedding_drift and not args.fast and not args.no_embedding_clusters
    )
    include_embedding_control = (
        include_embedding_drift and not args.fast and not args.no_embedding_control
    )
    skip_evaluation = bool(args.fast or args.no_evaluation)
    options: dict[str, object] = {
        "target_species_only": not args.all_species,
        "include_embedding_drift": include_embedding_drift,
        "include_embedding_clusters": include_embedding_clusters,
        "include_embedding_control": include_embedding_control,
        "include_sovon_context": not skip_evaluation and not args.no_sovon_context,
        "skip_evaluation": skip_evaluation,
        "include_environmental": bool(args.include_environmental),
        "model_streams": model_streams,
    }
    if species_ids is not None:
        options["species_ids"] = species_ids
    return options


def _refresh_upstream_derived_tables(
    *,
    start_date: date,
    end_date: date,
    refresh_scope: str,
) -> None:
    """Refresh derived tables needed by modeling."""
    logger.info(
        "Refreshing upstream derived tables before modeling: start_date=%s end_date=%s refresh_scope=%s.",
        start_date,
        end_date,
        refresh_scope,
    )
    refresh_species_dim_task()
    refresh_species_tracker_task(
        start_date=start_date,
        end_date=end_date,
        refresh_scope=refresh_scope,
    )


def _load_species_tracker_coverage(
    *,
    start_date: date | None = None,
    end_date: date | None = None,
    pool: ConnectionPool | None = None,
) -> SpeciesTrackerCoverage:
    """Load weekly grid coverage from species_tracker_tbl."""
    own_pool = pool is None
    pool = pool or ConnectionPool(min_conn=1, max_conn=1)
    try:
        with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """
                SELECT
                    MIN(period_start)::date AS min_period_start,
                    MAX(period_start)::date AS max_period_start,
                    COUNT(DISTINCT period_start) AS weekly_periods,
                    COUNT(*) AS rows
                FROM species_tracker_tbl
                WHERE row_kind IN ('species', 'community')
                  AND period_grain = 'week'
                  AND location_type = 'grid'
                  AND (%s::date IS NULL OR period_start >= %s)
                  AND (%s::date IS NULL OR period_start <= %s)
                """,
                (start_date, start_date, end_date, end_date),
            )
            row = cur.fetchone() or {}
    finally:
        if own_pool:
            pool.close()

    return SpeciesTrackerCoverage(
        min_period_start=row.get("min_period_start"),
        max_period_start=row.get("max_period_start"),
        weekly_periods=int(row.get("weekly_periods") or 0),
        rows=int(row.get("rows") or 0),
    )


def _tracker_coverage_reaches_end(
    coverage: SpeciesTrackerCoverage,
    *,
    end_date: date,
) -> bool:
    """Return whether coverage reaches the weekly period containing end_date."""
    expected_max_period_start = _week_start(end_date)
    return (
        coverage.max_period_start is not None
        and coverage.max_period_start >= expected_max_period_start
    )


def _iter_windows(
    start_date: date,
    end_date: date,
    *,
    window_days: int,
) -> list[tuple[date, date]]:
    """Return inclusive date windows for a backfill."""
    if end_date < start_date:
        msg = f"end date ({end_date}) must be >= start date ({start_date})"
        raise ValueError(msg)
    if window_days < 1:
        msg = "window_days must be >= 1"
        raise ValueError(msg)

    windows: list[tuple[date, date]] = []
    current = start_date
    while current <= end_date:
        window_end = min(current + timedelta(days=window_days - 1), end_date)
        windows.append((current, window_end))
        current = window_end + timedelta(days=1)
    return windows


def _iter_window_chunks(
    windows: Sequence[tuple[date, date]],
    *,
    chunk_windows: int,
) -> list[list[tuple[date, date]]]:
    """Return bounded groups of inclusive windows."""
    if chunk_windows < 1:
        msg = "chunk_windows must be >= 1"
        raise ValueError(msg)
    return [
        list(windows[index : index + chunk_windows])
        for index in range(0, len(windows), chunk_windows)
    ]


def _refresh_species_tracker_range(
    *,
    windows: Sequence[tuple[date, date]],
    refresh_scope: str,
    chunk_windows: int,
) -> None:
    """Refresh the full requested tracker range using bounded SQL chunks."""
    chunks = _iter_window_chunks(windows, chunk_windows=chunk_windows)
    if not chunks:
        return

    if len(chunks) == 1:
        chunk = chunks[0]
        refresh_species_tracker_task(
            start_date=chunk[0][0],
            end_date=chunk[-1][1],
            refresh_scope=refresh_scope,
        )
        return

    logger.info(
        "Refreshing species_tracker_tbl in %d chunk(s) of up to %d window(s).",
        len(chunks),
        chunk_windows,
    )
    for index, chunk in enumerate(chunks, start=1):
        chunk_start = chunk[0][0]
        chunk_end = chunk[-1][1]
        logger.info(
            "Refreshing species_tracker range chunk %d/%d: %s..%s.",
            index,
            len(chunks),
            chunk_start,
            chunk_end,
        )
        refresh_species_tracker_task(
            start_date=chunk_start,
            end_date=chunk_end,
            refresh_scope=refresh_scope,
        )


def _default_windowed_run_id(
    *,
    start_date: date,
    end_date: date,
    window_days: int,
    scoring_mode: str,
    preset: str,
    species_ids: tuple[str, ...] | None,
    target_species_only: bool,
    include_embedding_drift: bool,
    include_embedding_clusters: bool,
    include_embedding_control: bool,
) -> str:
    if species_ids:
        species_scope = "species_" + "_".join(species_ids)
    else:
        species_scope = "target" if target_species_only else "all_species"
    if not include_embedding_drift:
        embedding_scope = "no_embedding"
    else:
        cluster_scope = "clusters" if include_embedding_clusters else "no_clusters"
        control_scope = "control" if include_embedding_control else "no_control"
        embedding_scope = f"embedding_{cluster_scope}_{control_scope}"
    return (
        "anomaly_scoring_backfill_"
        f"{start_date:%Y%m%d}_{end_date:%Y%m%d}_"
        f"{window_days}d_{preset}_{scoring_mode}_{species_scope}_{embedding_scope}"
    )


def _window_marker_run_id(
    aggregate_run_id: str,
    *,
    start_date: date,
    end_date: date,
) -> str:
    return f"{aggregate_run_id}__window_{start_date:%Y%m%d}_{end_date:%Y%m%d}"


def _window_already_complete(
    *,
    aggregate_run_id: str,
    start_date: date,
    end_date: date,
    pool: ConnectionPool,
) -> bool:
    marker_run_id = _window_marker_run_id(
        aggregate_run_id,
        start_date=start_date,
        end_date=end_date,
    )
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT status
            FROM modeling_runs_tbl
            WHERE run_id = %s
              AND run_kind = %s
              AND status = 'complete'
            """,
            (marker_run_id, _WINDOW_MARKER_RUN_KIND),
        )
        if cur.fetchone():
            return True

        cur.execute(
            """
            SELECT COUNT(*) AS rows
            FROM modeling_species_week_scores_tbl
            WHERE run_id = %s
              AND period_start >= %s
              AND period_start <= %s
            """,
            (aggregate_run_id, start_date, end_date),
        )
        row = cur.fetchone() or {}
    return int(row.get("rows") or 0) > 0


def _mark_window_complete(
    *,
    aggregate_run_id: str,
    start_date: date,
    end_date: date,
    stats: ModelingRunStats,
    pool: ConnectionPool,
) -> None:
    marker_run_id = _window_marker_run_id(
        aggregate_run_id,
        start_date=start_date,
        end_date=end_date,
    )
    persist_modeling_run(
        run_id=marker_run_id,
        run_kind=_WINDOW_MARKER_RUN_KIND,
        status="complete",
        config={
            "aggregate_run_id": aggregate_run_id,
            "window_start_date": start_date.isoformat(),
            "window_end_date": end_date.isoformat(),
        },
        stats=stats.to_metadata(),
        source_coverage={
            "window": {
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
            },
        },
        pool=pool,
        replace_existing_run=False,
    )


def _load_aggregate_run_stats(
    *,
    run_id: str,
    pool: ConnectionPool,
) -> ModelingRunStats:
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                COUNT(*) AS rows_scored,
                COUNT(*) FILTER (WHERE alert_level = 'green') AS alerts_green,
                COUNT(*) FILTER (WHERE alert_level = 'amber') AS alerts_amber,
                COUNT(*) FILTER (WHERE alert_level = 'red') AS alerts_red,
                COUNT(*) FILTER (
                    WHERE COALESCE(payload->>'quality_mask_reason', '') LIKE '%%sparse_baseline%%'
                ) AS sparse_baseline_rows
            FROM modeling_species_week_scores_tbl
            WHERE run_id = %s
            """,
            (run_id,),
        )
        score_row = cur.fetchone() or {}
        cur.execute(
            """
            SELECT COUNT(*) AS feature_rows
            FROM modeling_species_week_features_tbl
            WHERE run_id = %s
            """,
            (run_id,),
        )
        feature_row = cur.fetchone() or {}
        cur.execute(
            """
            SELECT
                COUNT(*) AS cluster_rows,
                COUNT(*) FILTER (WHERE is_cluster_exemplar) AS cluster_exemplar_rows
            FROM modeling_embedding_clusters_tbl
            WHERE run_id = %s
            """,
            (run_id,),
        )
        cluster_row = cur.fetchone() or {}

    return ModelingRunStats(
        run_id=run_id,
        database_tables=(
            "modeling_runs_tbl, modeling_species_week_features_tbl, "
            "modeling_species_week_scores_tbl, modeling_embedding_clusters_tbl"
        ),
        rows_loaded=0,
        feature_rows=int(feature_row.get("feature_rows") or 0),
        rows_scored=int(score_row.get("rows_scored") or 0),
        alerts_green=int(score_row.get("alerts_green") or 0),
        alerts_amber=int(score_row.get("alerts_amber") or 0),
        alerts_red=int(score_row.get("alerts_red") or 0),
        sparse_baseline_rows=int(score_row.get("sparse_baseline_rows") or 0),
        cluster_rows=int(cluster_row.get("cluster_rows") or 0),
        cluster_exemplar_rows=int(cluster_row.get("cluster_exemplar_rows") or 0),
    )


def _finalize_windowed_run(
    *,
    run_id: str,
    stats: ModelingRunStats,
    config: dict[str, object],
    windows_total: int,
    windows_processed: int,
    windows_skipped: int,
    model_passes: int,
    pool: ConnectionPool,
) -> None:
    persist_modeling_run(
        run_id=run_id,
        run_kind="anomaly_scoring",
        status="complete",
        config=config,
        stats={
            **stats.to_metadata(),
            "windows_total": windows_total,
            "windows_processed": windows_processed,
            "windows_skipped": windows_skipped,
            "model_passes": model_passes,
        },
        source_coverage={
            "windowed_backfill": {
                "windows_total": windows_total,
                "windows_processed": windows_processed,
                "windows_skipped": windows_skipped,
                "model_passes": model_passes,
            },
        },
        pool=pool,
        replace_existing_run=False,
    )


def _run_windowed_backfill(args: argparse.Namespace) -> dict[str, object]:
    start_date = args.refresh_start_date
    end_date = args.refresh_end_date
    windows = _iter_windows(
        start_date,
        end_date,
        window_days=args.window_days,
    )
    options = _modeling_options(args)
    target_species_only = bool(options["target_species_only"])
    include_embedding_drift = bool(options["include_embedding_drift"])
    include_embedding_clusters = bool(options["include_embedding_clusters"])
    include_embedding_control = bool(options["include_embedding_control"])
    include_sovon_context = bool(options["include_sovon_context"])
    skip_evaluation = bool(options["skip_evaluation"])
    include_environmental = bool(options["include_environmental"])
    model_streams = tuple(options["model_streams"])
    species_ids = options.get("species_ids")
    selected_species_ids = tuple(species_ids) if species_ids else None
    run_id = args.run_id or _default_windowed_run_id(
        start_date=start_date,
        end_date=end_date,
        window_days=args.window_days,
        scoring_mode=args.scoring_mode,
        preset=args.preset,
        species_ids=selected_species_ids,
        target_species_only=target_species_only,
        include_embedding_drift=include_embedding_drift,
        include_embedding_clusters=include_embedding_clusters,
        include_embedding_control=include_embedding_control,
    )
    history_start_date = args.history_start_date or start_date
    derived_refresh_mode = (
        "none" if args.no_derived_refresh else args.derived_refresh_mode
    )
    refresh_derived = derived_refresh_mode != "none"

    requested_scoring_batch_windows = 0 if args.fast else args.scoring_batch_windows
    scoring_batch_size = (
        len(windows)
        if requested_scoring_batch_windows == 0
        else requested_scoring_batch_windows
    )
    if scoring_batch_size < 1:
        msg = "scoring_batch_windows must be >= 0"
        raise ValueError(msg)
    expected_model_passes = (
        (len(windows) + scoring_batch_size - 1) // scoring_batch_size if windows else 0
    )
    embedding_preload_enabled = (
        include_embedding_drift and scoring_batch_size > 1 and expected_model_passes > 1
    )
    logger.info(
        "Starting windowed modeling backfill: run_id=%s windows=%d start_date=%s "
        "end_date=%s window_days=%d derived_refresh_mode=%s "
        "scoring_batch_windows=%d expected_model_passes=%d fast=%s "
        "include_embedding_clusters=%s include_embedding_control=%s "
        "include_sovon_context=%s skip_evaluation=%s "
        "include_environmental=%s model_streams=%s species_ids=%s.",
        run_id,
        len(windows),
        start_date,
        end_date,
        args.window_days,
        derived_refresh_mode,
        scoring_batch_size,
        expected_model_passes,
        args.fast,
        include_embedding_clusters,
        include_embedding_control,
        include_sovon_context,
        skip_evaluation,
        include_environmental,
        ",".join(model_streams),
        ",".join(selected_species_ids or ()),
    )
    if include_embedding_drift and not embedding_preload_enabled:
        logger.info(
            "Embedding clip preload disabled; each modeling pass will load only "
            "its bounded source range."
        )

    run_started = perf_counter()
    pool = ConnectionPool(min_conn=1, max_conn=3)
    windows_processed = 0
    windows_skipped = 0
    model_passes = 0
    preloaded_embedding_clip_rows: pd.DataFrame | None = None
    preloaded_control_embedding_clip_rows: pd.DataFrame | None = None
    embedding_preload_complete = False
    try:
        if refresh_derived:
            species_dim_started = perf_counter()
            refresh_species_dim_task()
            logger.info(
                "species_tbl refresh finished in %.2fs.",
                perf_counter() - species_dim_started,
            )
        if derived_refresh_mode == "range":
            logger.info(
                "Refreshing species_tracker_tbl for full modeling backfill range %s..%s.",
                start_date,
                end_date,
            )
            tracker_refresh_started = perf_counter()
            refresh_chunk_windows = min(
                scoring_batch_size,
                _DEFAULT_DERIVED_REFRESH_CHUNK_WINDOWS,
            )
            _refresh_species_tracker_range(
                windows=windows,
                refresh_scope=args.refresh_scope,
                chunk_windows=refresh_chunk_windows,
            )
            logger.info(
                "species_tracker range refresh finished in %.2fs.",
                perf_counter() - tracker_refresh_started,
            )
            coverage = _load_species_tracker_coverage(
                start_date=_week_start(start_date),
                end_date=_week_start(end_date),
                pool=pool,
            )
            logger.info(
                "species_tracker weekly grid coverage after range refresh: %s",
                json.dumps(coverage.to_log_payload(), sort_keys=True),
            )
            if not _tracker_coverage_reaches_end(
                coverage,
                end_date=_week_start(end_date),
            ):
                msg = (
                    "species_tracker coverage is incomplete after range refresh: "
                    f"max_period_start={coverage.max_period_start} "
                    f"expected_at_least={_week_start(end_date)}"
                )
                raise RuntimeError(msg)

        pending_batch: list[tuple[int, date, date, date, date]] = []

        def ensure_embedding_preload() -> None:
            nonlocal embedding_preload_complete
            nonlocal preloaded_control_embedding_clip_rows
            nonlocal preloaded_embedding_clip_rows
            if (
                embedding_preload_complete
                or not include_embedding_drift
                or not embedding_preload_enabled
            ):
                return

            preload_start = _week_start(history_start_date)
            preload_end = _week_start(end_date)
            min_confidence = float(SETTINGS.embedding_drift["min_confidence"])
            logger.info(
                "Preloading embedding clip rows once for windowed backfill: %s..%s.",
                preload_start,
                preload_end,
            )
            preload_started = perf_counter()
            preloaded_embedding_clip_rows = _load_embedding_clip_rows(
                pool,
                target_species_only=target_species_only,
                species_ids=selected_species_ids,
                min_confidence=min_confidence,
                start_date=preload_start,
                end_date=preload_end,
            )
            if include_embedding_control:
                if target_species_only:
                    preloaded_control_embedding_clip_rows = _load_embedding_clip_rows(
                        pool,
                        target_species_only=False,
                        min_confidence=min_confidence,
                        start_date=preload_start,
                        end_date=preload_end,
                    )
                else:
                    preloaded_control_embedding_clip_rows = (
                        preloaded_embedding_clip_rows
                    )
            embedding_preload_complete = True
            logger.info(
                "Embedding clip preload finished in %.2fs: target_rows=%d control_rows=%d.",
                perf_counter() - preload_started,
                0
                if preloaded_embedding_clip_rows is None
                else len(preloaded_embedding_clip_rows),
                0
                if preloaded_control_embedding_clip_rows is None
                else len(preloaded_control_embedding_clip_rows),
            )

        def flush_pending_batch() -> None:
            nonlocal model_passes, pending_batch, windows_processed
            if not pending_batch:
                return

            batch_first = pending_batch[0]
            batch_last = pending_batch[-1]
            batch_label = (
                f"[{batch_first[0]}-{batch_last[0]}/{len(windows)}] "
                f"{batch_first[1]}..{batch_last[2]}"
            )
            logger.info(
                "%s scoring %d pending modeling window(s) in one pass.",
                batch_label,
                len(pending_batch),
            )
            batch_started = perf_counter()
            ensure_embedding_preload()
            model_kwargs: dict[str, object] = {
                "scoring_mode": args.scoring_mode,
                "target_species_only": target_species_only,
                "species_ids": selected_species_ids,
                "include_embedding_drift": include_embedding_drift,
                "include_embedding_clusters": include_embedding_clusters,
                "include_embedding_control": include_embedding_control,
                "include_sovon_context": include_sovon_context,
                "skip_evaluation": skip_evaluation,
                "include_environmental": include_environmental,
                "model_streams": model_streams,
                "source_start_date": _week_start(history_start_date),
                "source_end_date": batch_last[4],
                "output_period_start": batch_first[3],
                "output_period_end": batch_last[4],
                "run_id": run_id,
                "run_status": "running",
                "replace_existing_run": False,
                "pool": pool,
            }
            if embedding_preload_complete:
                model_kwargs["preloaded_embedding_clip_rows"] = (
                    preloaded_embedding_clip_rows
                )
                model_kwargs["preloaded_control_embedding_clip_rows"] = (
                    preloaded_control_embedding_clip_rows
                )
            stats = run_model_anomaly_scoring(**model_kwargs)
            for (
                _index,
                window_start,
                window_end,
                _period_start,
                _period_end,
            ) in pending_batch:
                _mark_window_complete(
                    aggregate_run_id=run_id,
                    start_date=window_start,
                    end_date=window_end,
                    stats=stats,
                    pool=pool,
                )
            windows_processed += len(pending_batch)
            model_passes += 1
            logger.info(
                "%s complete in %.2fs: rows_loaded=%d feature_rows=%d rows_scored=%d.",
                batch_label,
                perf_counter() - batch_started,
                stats.rows_loaded,
                stats.feature_rows,
                stats.rows_scored,
            )
            pending_batch = []

        for index, (window_start, window_end) in enumerate(windows, start=1):
            label = f"[{index}/{len(windows)}] {window_start}..{window_end}"
            target_period_start = _week_start(window_start)
            target_period_end = _week_start(window_end)
            if not args.force and _window_already_complete(
                aggregate_run_id=run_id,
                start_date=target_period_start,
                end_date=target_period_end,
                pool=pool,
            ):
                flush_pending_batch()
                windows_skipped += 1
                logger.info("%s already complete; skipping.", label)
                continue

            if derived_refresh_mode == "window":
                logger.info("%s refreshing and queueing modeling window.", label)
            else:
                logger.info("%s queueing modeling window.", label)
            if derived_refresh_mode == "window":
                tracker_refresh_started = perf_counter()
                refresh_species_tracker_task(
                    start_date=window_start,
                    end_date=window_end,
                    refresh_scope=args.refresh_scope,
                )
                logger.info(
                    "%s species_tracker window refresh finished in %.2fs.",
                    label,
                    perf_counter() - tracker_refresh_started,
                )
                coverage = _load_species_tracker_coverage(
                    start_date=target_period_start,
                    end_date=target_period_end,
                    pool=pool,
                )
                logger.info(
                    "%s species_tracker weekly grid coverage: %s",
                    label,
                    json.dumps(coverage.to_log_payload(), sort_keys=True),
                )
                if not _tracker_coverage_reaches_end(
                    coverage,
                    end_date=target_period_end,
                ):
                    msg = (
                        f"{label} species_tracker coverage is incomplete after "
                        f"refresh: max_period_start={coverage.max_period_start} "
                        f"expected_at_least={target_period_end}"
                    )
                    raise RuntimeError(msg)
            elif refresh_derived:
                coverage = _load_species_tracker_coverage(
                    start_date=target_period_start,
                    end_date=target_period_end,
                    pool=pool,
                )
                logger.info(
                    "%s species_tracker weekly grid coverage: %s",
                    label,
                    json.dumps(coverage.to_log_payload(), sort_keys=True),
                )
                if not _tracker_coverage_reaches_end(
                    coverage,
                    end_date=target_period_end,
                ):
                    msg = (
                        f"{label} species_tracker coverage is incomplete: "
                        f"max_period_start={coverage.max_period_start} "
                        f"expected_at_least={target_period_end}"
                    )
                    raise RuntimeError(msg)

            pending_batch.append(
                (
                    index,
                    window_start,
                    window_end,
                    target_period_start,
                    target_period_end,
                )
            )
            if len(pending_batch) >= scoring_batch_size:
                flush_pending_batch()

        flush_pending_batch()

        aggregate_stats = _load_aggregate_run_stats(run_id=run_id, pool=pool)
        config = {
            "windowed_backfill": True,
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "window_days": args.window_days,
            "scoring_batch_windows": scoring_batch_size,
            "fast": args.fast,
            "history_start_date": history_start_date.isoformat(),
            "scoring_mode": args.scoring_mode,
            "preset": args.preset,
            "target_species_only": target_species_only,
            "species_ids": list(selected_species_ids) if selected_species_ids else None,
            "include_embedding_drift": include_embedding_drift,
            "include_embedding_clusters": include_embedding_clusters,
            "include_embedding_control": include_embedding_control,
            "include_sovon_context": include_sovon_context,
            "skip_evaluation": skip_evaluation,
            "include_environmental": include_environmental,
            "model_streams": list(model_streams),
            "derived_refresh_mode": derived_refresh_mode,
            "refresh_derived": refresh_derived,
            "refresh_scope": args.refresh_scope,
        }
        _finalize_windowed_run(
            run_id=run_id,
            stats=aggregate_stats,
            config=config,
            windows_total=len(windows),
            windows_processed=windows_processed,
            windows_skipped=windows_skipped,
            model_passes=model_passes,
            pool=pool,
        )
    finally:
        pool.close()

    return {
        **aggregate_stats.to_metadata(),
        "windows_total": len(windows),
        "windows_processed": windows_processed,
        "windows_skipped": windows_skipped,
        "model_passes": model_passes,
        "elapsed_seconds": round(perf_counter() - run_started, 3),
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s [%(levelname)s] %(message)s",
    )
    if args.windowed:
        payload = _run_windowed_backfill(args)
        logger.info("Windowed modeling backfill complete: %s", json.dumps(payload))
        sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
        return 0

    logger.info("Starting persisted modeling backfill.")
    if args.refresh_derived:
        _refresh_upstream_derived_tables(
            start_date=args.refresh_start_date,
            end_date=args.refresh_end_date,
            refresh_scope=args.refresh_scope,
        )
        coverage = _load_species_tracker_coverage()
        logger.info(
            "species_tracker weekly grid coverage after refresh: %s",
            json.dumps(coverage.to_log_payload(), sort_keys=True),
        )
        if not _tracker_coverage_reaches_end(
            coverage,
            end_date=args.refresh_end_date,
        ):
            logger.error(
                "species_tracker coverage is incomplete for modeling: max_period_start=%s expected_at_least=%s.",
                coverage.max_period_start,
                _week_start(args.refresh_end_date),
            )
            return 1

    options = _modeling_options(args)
    kwargs: dict[str, object] = {
        "scoring_mode": args.scoring_mode,
        **options,
    }

    stats = run_model_anomaly_scoring(**kwargs)
    payload = stats.to_metadata()
    logger.info("Persisted modeling backfill complete: %s", json.dumps(payload))
    sys.stdout.write(json.dumps(payload, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
