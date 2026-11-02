"""Database persistence helpers for modeling-phase outputs."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
import psycopg2.extras
from psycopg2 import sql

from avian_biosurveillance.db.database import ConnectionPool, get_pool
from avian_biosurveillance.db.loaders._util import chunked


logger = logging.getLogger(__name__)

DEFAULT_CHUNK_SIZE = 2_000

_RUN_DATA_TABLES = (
    "modeling_species_week_features_tbl",
    "modeling_species_week_scores_tbl",
    "modeling_embedding_clusters_tbl",
)

_RUN_DATA_PERIOD_COLUMNS = {
    "modeling_species_week_features_tbl": "period_start",
    "modeling_species_week_scores_tbl": "period_start",
    "modeling_embedding_clusters_tbl": "period_start",
}

_RUN_SQL = """
INSERT INTO modeling_runs_tbl (
    run_id,
    run_kind,
    status,
    generated_at,
    completed_at,
    parent_tuning_run_id,
    config,
    stats,
    source_coverage,
    validation_report,
    quality_report,
    payload
) VALUES %s
ON CONFLICT (run_id) DO UPDATE SET
    run_kind = EXCLUDED.run_kind,
    status = EXCLUDED.status,
    generated_at = EXCLUDED.generated_at,
    completed_at = EXCLUDED.completed_at,
    parent_tuning_run_id = EXCLUDED.parent_tuning_run_id,
    config = EXCLUDED.config,
    stats = EXCLUDED.stats,
    source_coverage = EXCLUDED.source_coverage,
    validation_report = EXCLUDED.validation_report,
    quality_report = EXCLUDED.quality_report,
    payload = EXCLUDED.payload,
    computed_at = NOW()
"""

_FEATURE_SQL = """
INSERT INTO modeling_species_week_features_tbl (
    run_id,
    species_id,
    species_english,
    location_type,
    location_id,
    grid_cell_id,
    period_start,
    period_end,
    month_start,
    total_detections,
    active_days,
    species_station_count,
    community_total_detections,
    community_active_days,
    community_active_station_count,
    payload
) VALUES %s
"""

_SCORE_SQL = """
INSERT INTO modeling_species_week_scores_tbl (
    run_id,
    species_id,
    species_english,
    location_type,
    location_id,
    grid_cell_id,
    period_start,
    period_end,
    month_start,
    alert_level,
    model_eligible,
    model_fit_status,
    model_anomaly_score,
    final_score,
    population_anomaly_score,
    population_drop_score,
    population_spike_score,
    embedding_anomaly_score,
    embedding_drift_score,
    embedding_outlier_share,
    embedding_clip_count_scored,
    top_detection_ids,
    anomaly_streams,
    payload
) VALUES %s
"""

_CLUSTER_SQL = """
INSERT INTO modeling_embedding_clusters_tbl (
    run_id,
    detection_id,
    species_id,
    location_type,
    location_id,
    station_id,
    grid_cell_id,
    period_start,
    period_end,
    embedding_model,
    cluster_id,
    cluster_label,
    cluster_rank,
    cluster_size,
    cluster_count,
    cluster_member_share,
    cluster_centroid_distance,
    cluster_distance_threshold,
    cluster_quality,
    is_cluster_exemplar,
    cluster_exemplar_detection_id,
    payload
) VALUES %s
"""

_TUNING_RUN_SQL = """
INSERT INTO modeling_tuning_runs_tbl (
    tuning_run_id,
    generated_at,
    sweep_mode,
    objective_weights,
    negative_control_penalty,
    top_k,
    config_count,
    best_config_label,
    best_objective,
    best_config_overrides,
    payload
) VALUES %s
ON CONFLICT (tuning_run_id) DO UPDATE SET
    generated_at = EXCLUDED.generated_at,
    sweep_mode = EXCLUDED.sweep_mode,
    objective_weights = EXCLUDED.objective_weights,
    negative_control_penalty = EXCLUDED.negative_control_penalty,
    top_k = EXCLUDED.top_k,
    config_count = EXCLUDED.config_count,
    best_config_label = EXCLUDED.best_config_label,
    best_objective = EXCLUDED.best_objective,
    best_config_overrides = EXCLUDED.best_config_overrides,
    payload = EXCLUDED.payload,
    computed_at = NOW()
"""

_TUNING_ROW_SQL = """
INSERT INTO modeling_tuning_leaderboard_rows_tbl (
    tuning_run_id,
    leaderboard_rank,
    config_index,
    config_label,
    objective,
    overrides,
    payload
) VALUES %s
"""


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        result = pd.isna(value)
    except (TypeError, ValueError):
        return False
    if isinstance(result, (bool, np.bool_)):
        return bool(result)
    return False


def _json_ready(value: Any) -> Any:
    """Convert pandas/numpy values into JSONB-safe Python primitives."""
    if _is_missing(value):
        return None
    if isinstance(value, dict):
        return {str(k): _json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_ready(v) for v in value]
    if isinstance(value, pd.Timestamp):
        if value.tzinfo is None:
            return value.isoformat()
        return value.isoformat()
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        number = float(value)
        return None if math.isnan(number) else number
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, float) and math.isnan(value):
        return None
    return value


def _payload(record: Mapping[str, Any]) -> psycopg2.extras.Json:
    return psycopg2.extras.Json(_json_ready(dict(record)))


def _records(frame: pd.DataFrame | None) -> list[dict[str, Any]]:
    if frame is None or frame.empty:
        return []
    return [
        {str(key): _json_ready(value) for key, value in record.items()}
        for record in frame.to_dict(orient="records")
    ]


def _text(record: Mapping[str, Any], key: str) -> str | None:
    value = record.get(key)
    if _is_missing(value):
        return None
    text = str(value).strip()
    return text or None


def _date(record: Mapping[str, Any], key: str) -> date | None:
    value = record.get(key)
    if _is_missing(value):
        return None
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def _float(record: Mapping[str, Any], key: str) -> float | None:
    value = record.get(key)
    if _is_missing(value):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _int(record: Mapping[str, Any], key: str) -> int | None:
    value = record.get(key)
    if _is_missing(value):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return int(parsed) if math.isfinite(parsed) else None


def _bool(record: Mapping[str, Any], key: str) -> bool | None:
    value = record.get(key)
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"true", "t", "1", "yes", "y"}:
        return True
    if text in {"false", "f", "0", "no", "n"}:
        return False
    return None


def _list(record: Mapping[str, Any], key: str) -> list[str]:
    value = record.get(key)
    if _is_missing(value):
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


def _execute_values(
    cur: Any,
    sql: str,
    values: Sequence[tuple[Any, ...]],
    *,
    chunk_size: int,
) -> int:
    total = 0
    for part in chunked(values, chunk_size):
        psycopg2.extras.execute_values(cur, sql, part)
        total += len(part)
    return total


def _feature_values(
    run_id: str, rows: Sequence[Mapping[str, Any]]
) -> list[tuple[Any, ...]]:
    return [
        (
            run_id,
            _text(row, "species_id"),
            _text(row, "species_english"),
            _text(row, "location_type"),
            _text(row, "location_id"),
            _text(row, "grid_cell_id") or _text(row, "location_id"),
            _date(row, "period_start"),
            _date(row, "period_end"),
            _date(row, "month_start"),
            _int(row, "total_detections"),
            _int(row, "active_days"),
            _int(row, "species_station_count"),
            _int(row, "community_total_detections"),
            _int(row, "community_active_days"),
            _int(row, "community_active_station_count"),
            _payload(row),
        )
        for row in rows
    ]


def _score_values(
    run_id: str, rows: Sequence[Mapping[str, Any]]
) -> list[tuple[Any, ...]]:
    return [
        (
            run_id,
            _text(row, "species_id"),
            _text(row, "species_english"),
            _text(row, "location_type"),
            _text(row, "location_id"),
            _text(row, "grid_cell_id") or _text(row, "location_id"),
            _date(row, "period_start"),
            _date(row, "period_end"),
            _date(row, "month_start"),
            _text(row, "alert_level"),
            _bool(row, "model_eligible"),
            _text(row, "model_fit_status"),
            _float(row, "model_anomaly_score"),
            _float(row, "final_score"),
            _float(row, "population_anomaly_score"),
            _float(row, "population_drop_score"),
            _float(row, "population_spike_score"),
            _float(row, "embedding_anomaly_score"),
            _float(row, "embedding_drift_score"),
            _float(row, "embedding_outlier_share"),
            _int(row, "embedding_clip_count_scored"),
            _list(row, "top_detection_ids"),
            _list(row, "anomaly_streams"),
            _payload(row),
        )
        for row in rows
    ]


def _cluster_values(
    run_id: str, rows: Sequence[Mapping[str, Any]]
) -> list[tuple[Any, ...]]:
    return [
        (
            run_id,
            _text(row, "detection_id"),
            _text(row, "species_id"),
            _text(row, "location_type"),
            _text(row, "location_id"),
            _text(row, "station_id"),
            _text(row, "grid_cell_id"),
            _date(row, "period_start"),
            _date(row, "period_end"),
            _text(row, "embedding_model"),
            _text(row, "cluster_id"),
            _text(row, "cluster_label"),
            _int(row, "cluster_rank"),
            _int(row, "cluster_size"),
            _int(row, "cluster_count"),
            _float(row, "cluster_member_share"),
            _float(row, "cluster_centroid_distance"),
            _float(row, "cluster_distance_threshold"),
            _text(row, "cluster_quality"),
            _bool(row, "is_cluster_exemplar"),
            _text(row, "cluster_exemplar_detection_id"),
            _payload(row),
        )
        for row in rows
    ]


def persist_modeling_run(
    *,
    run_id: str,
    run_kind: str,
    status: str = "complete",
    generated_at: datetime | None = None,
    completed_at: datetime | None = None,
    parent_tuning_run_id: str | None = None,
    config: Mapping[str, Any] | None = None,
    stats: Mapping[str, Any] | None = None,
    source_coverage: Mapping[str, Any] | None = None,
    validation_report: Mapping[str, Any] | None = None,
    quality_report: Mapping[str, Any] | None = None,
    features: pd.DataFrame | None = None,
    scores: pd.DataFrame | None = None,
    clusters: pd.DataFrame | None = None,
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    replace_existing_run: bool = True,
    replace_period_start: date | None = None,
    replace_period_end: date | None = None,
) -> dict[str, int]:
    """Persist a full modeling run and replace any existing rows for the run."""
    pool = pool or get_pool()
    generated_at = generated_at or datetime.now().astimezone()
    if completed_at is None and status == "complete":
        completed_at = generated_at
    if (replace_period_start is None) != (replace_period_end is None):
        msg = "replace_period_start and replace_period_end must be provided together"
        raise ValueError(msg)

    feature_rows = _records(features)
    score_rows = _records(scores)
    cluster_rows = _records(clusters)

    counts = {
        "feature_rows": len(feature_rows),
        "score_rows": len(score_rows),
        "cluster_rows": len(cluster_rows),
    }

    with pool.connection() as conn:
        try:
            with conn.cursor() as cur:
                _delete_existing_run_rows(
                    cur,
                    run_id=run_id,
                    replace_existing_run=replace_existing_run,
                    replace_period_start=replace_period_start,
                    replace_period_end=replace_period_end,
                )

                run_payload = {
                    "run_id": run_id,
                    "run_kind": run_kind,
                    "status": status,
                    "generated_at": generated_at,
                    "completed_at": completed_at,
                    "parent_tuning_run_id": parent_tuning_run_id,
                    "config": dict(config or {}),
                    "stats": dict(stats or {}),
                    "source_coverage": dict(source_coverage or {}),
                    "validation_report": validation_report,
                    "quality_report": quality_report,
                    "row_counts": counts,
                }
                psycopg2.extras.execute_values(
                    cur,
                    _RUN_SQL,
                    [
                        (
                            run_id,
                            run_kind,
                            status,
                            generated_at,
                            completed_at,
                            parent_tuning_run_id,
                            psycopg2.extras.Json(_json_ready(config or {})),
                            psycopg2.extras.Json(_json_ready(stats or {})),
                            psycopg2.extras.Json(_json_ready(source_coverage or {})),
                            (
                                psycopg2.extras.Json(_json_ready(validation_report))
                                if validation_report is not None
                                else None
                            ),
                            (
                                psycopg2.extras.Json(_json_ready(quality_report))
                                if quality_report is not None
                                else None
                            ),
                            psycopg2.extras.Json(_json_ready(run_payload)),
                        )
                    ],
                )

                _execute_values(
                    cur,
                    _FEATURE_SQL,
                    _feature_values(run_id, feature_rows),
                    chunk_size=chunk_size,
                )
                _execute_values(
                    cur,
                    _SCORE_SQL,
                    _score_values(run_id, score_rows),
                    chunk_size=chunk_size,
                )
                _execute_values(
                    cur,
                    _CLUSTER_SQL,
                    _cluster_values(run_id, cluster_rows),
                    chunk_size=chunk_size,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    logger.info(
        "modeling_runs_tbl: persisted run %s (%s score rows, %s feature rows).",
        run_id,
        counts["score_rows"],
        counts["feature_rows"],
    )
    return counts


def _delete_existing_run_rows(
    cur: Any,
    *,
    run_id: str,
    replace_existing_run: bool,
    replace_period_start: date | None,
    replace_period_end: date | None,
) -> None:
    """Delete existing row payloads before re-persisting a run/window."""
    if replace_existing_run:
        for table in _RUN_DATA_TABLES:
            cur.execute(
                sql.SQL("DELETE FROM {} WHERE run_id = %s").format(
                    sql.Identifier(table)
                ),
                (run_id,),
            )
        return

    if replace_period_start is None or replace_period_end is None:
        return

    for table, period_column in _RUN_DATA_PERIOD_COLUMNS.items():
        cur.execute(
            sql.SQL(
                """
                DELETE FROM {}
                WHERE run_id = %s
                  AND {} >= %s
                  AND {} <= %s
                """
            ).format(
                sql.Identifier(table),
                sql.Identifier(period_column),
                sql.Identifier(period_column),
            ),
            (run_id, replace_period_start, replace_period_end),
        )


def update_modeling_validation_report(
    *,
    run_id: str,
    validation_report: Mapping[str, Any],
    stats: Mapping[str, Any] | None = None,
    pool: ConnectionPool | None = None,
) -> None:
    """Update the validation report JSONB for an existing modeling run."""
    pool = pool or get_pool()
    with pool.cursor() as cur:
        if stats is None:
            cur.execute(
                """
                UPDATE modeling_runs_tbl
                SET validation_report = %s, computed_at = NOW()
                WHERE run_id = %s
                """,
                (psycopg2.extras.Json(_json_ready(validation_report)), run_id),
            )
        else:
            cur.execute(
                """
                UPDATE modeling_runs_tbl
                SET validation_report = %s, stats = %s, computed_at = NOW()
                WHERE run_id = %s
                """,
                (
                    psycopg2.extras.Json(_json_ready(validation_report)),
                    psycopg2.extras.Json(_json_ready(stats)),
                    run_id,
                ),
            )


def load_latest_modeling_run(
    *,
    run_kind: str = "anomaly_scoring",
    pool: ConnectionPool | None = None,
) -> dict[str, Any] | None:
    """Return the latest complete modeling run envelope."""
    pool = pool or get_pool()
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM modeling_runs_tbl
            WHERE run_kind = %s
              AND status = 'complete'
            ORDER BY generated_at DESC
            LIMIT 1
            """,
            (run_kind,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def load_modeling_run(
    *,
    run_id: str,
    pool: ConnectionPool | None = None,
) -> dict[str, Any] | None:
    """Return a modeling run envelope by id."""
    pool = pool or get_pool()
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT *
            FROM modeling_runs_tbl
            WHERE run_id = %s
            """,
            (run_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def load_score_payload_frame(
    *,
    run_id: str,
    pool: ConnectionPool | None = None,
) -> pd.DataFrame:
    """Load score payload rows for a persisted run as a pandas DataFrame."""
    pool = pool or get_pool()
    with pool.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT payload
            FROM modeling_species_week_scores_tbl
            WHERE run_id = %s
            ORDER BY period_start, species_id, location_id
            """,
            (run_id,),
        )
        rows = [dict(record["payload"]) for record in cur.fetchall()]
    return pd.DataFrame.from_records(rows)


def persist_tuning_leaderboard(
    *,
    tuning_run_id: str,
    generated_at: datetime,
    mode: str,
    objective_weights: Mapping[str, Any],
    negative_control_penalty: float,
    top_k: int,
    leaderboard: pd.DataFrame,
    payload: Mapping[str, Any],
    pool: ConnectionPool | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> int:
    """Persist a tuning leaderboard and replace rows for the tuning run."""
    pool = pool or get_pool()
    records = _records(leaderboard)
    best = records[0] if records else {}
    with pool.connection() as conn:
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    DELETE FROM modeling_tuning_leaderboard_rows_tbl
                    WHERE tuning_run_id = %s
                    """,
                    (tuning_run_id,),
                )
                psycopg2.extras.execute_values(
                    cur,
                    _TUNING_RUN_SQL,
                    [
                        (
                            tuning_run_id,
                            generated_at,
                            mode,
                            psycopg2.extras.Json(_json_ready(objective_weights)),
                            negative_control_penalty,
                            top_k,
                            len(records),
                            _text(best, "config_label"),
                            _float(best, "objective"),
                            psycopg2.extras.Json(
                                _json_ready(
                                    json.loads(_text(best, "overrides") or "{}")
                                )
                            ),
                            psycopg2.extras.Json(_json_ready(payload)),
                        )
                    ],
                )
                values = [
                    (
                        tuning_run_id,
                        _int(row, "rank"),
                        _int(row, "config_index"),
                        _text(row, "config_label"),
                        _float(row, "objective"),
                        psycopg2.extras.Json(
                            _json_ready(json.loads(_text(row, "overrides") or "{}"))
                        ),
                        _payload(row),
                    )
                    for row in records
                ]
                _execute_values(
                    cur,
                    _TUNING_ROW_SQL,
                    values,
                    chunk_size=chunk_size,
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
    logger.info(
        "modeling_tuning_runs_tbl: persisted tuning run %s (%d rows).",
        tuning_run_id,
        len(records),
    )
    return len(records)


__all__ = [
    "load_latest_modeling_run",
    "load_modeling_run",
    "load_score_payload_frame",
    "persist_modeling_run",
    "persist_tuning_leaderboard",
    "update_modeling_validation_report",
]
