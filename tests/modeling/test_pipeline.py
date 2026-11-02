"""Tests for modeling pipeline database persistence."""

from __future__ import annotations

import inspect
import logging
from datetime import date
from typing import Any

import pandas as pd

import avian_biosurveillance.modeling.pipeline as pipeline_module
from avian_biosurveillance.modeling.pipeline import (
    _filter_embedding_clip_rows,
    _filter_period_window,
    _log_weekly_frame_coverage,
    _persist_outputs,
)


def _scored_frame() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "species_id": "35",
                "location_type": "network",
                "location_id": "nl",
                "period_start": "2026-03-02",
                "period_end": "2026-03-08",
                "model_anomaly_score": 0.99,
                "final_score": 0.99,
                "alert_level": "red",
                "quality_mask_reason": "ok",
                "model_feature_columns": "population_anomaly_score",
            }
        ]
    )


def test_species_week_loader_qualifies_grid_system_column() -> None:
    source = inspect.getsource(pipeline_module._load_species_week_rows)

    assert "species_tracker_tbl.grid_system AS grid_system" in source
    assert "\n            grid_system,\n" not in source


def test_embedding_clip_loader_filters_coordinate_missing_stations_in_sql() -> None:
    source = inspect.getsource(pipeline_module._load_embedding_clip_rows)

    assert "JOIN birdweather_stations_tbl s" in source
    assert "AND s.lat IS NOT NULL" in source
    assert "AND s.lon IS NOT NULL" in source
    assert "LEFT JOIN birdweather_stations_tbl" not in source


def test_filter_embedding_clip_rows_matches_source_window_bounds() -> None:
    rows = pd.DataFrame(
        [
            {
                "detection_id": "before",
                "detected_at": "2024-01-07T23:59:00Z",
                "observation_date": date(2024, 1, 7),
            },
            {
                "detection_id": "inside",
                "detected_at": "2024-01-08T06:00:00Z",
                "observation_date": date(2024, 1, 8),
            },
            {
                "detection_id": "end-day",
                "detected_at": "2024-01-15T23:59:00Z",
                "observation_date": date(2024, 1, 15),
            },
            {
                "detection_id": "after",
                "detected_at": "2024-01-16T00:00:00Z",
                "observation_date": date(2024, 1, 16),
            },
        ]
    )

    filtered = _filter_embedding_clip_rows(
        rows,
        start_date=date(2024, 1, 8),
        end_date=date(2024, 1, 15),
    )

    assert filtered["detection_id"].tolist() == ["inside", "end-day"]


def test_persist_outputs_writes_modeling_run_to_database(
    monkeypatch,
) -> None:
    scored = _scored_frame()
    features = pd.DataFrame(
        [
            {
                "species_id": "35",
                "location_type": "network",
                "location_id": "nl",
                "period_start": "2026-03-02",
                "period_end": "2026-03-08",
                "feature_set_version": "species_week_v1",
            }
        ]
    )
    source_week_rows = pd.DataFrame(
        [
            {
                "species_id": "35",
                "location_type": "network",
                "location_id": "nl",
                "period_start": "2026-03-02",
                "period_end": "2026-03-08",
            }
        ]
    )
    embedding_clip_rows = pd.DataFrame(
        [{"detection_id": "clip-1", "detected_at": "2026-03-04T12:15:00Z"}]
    )
    embedding_clusters = pd.DataFrame(
        [
            {
                "detection_id": "clip-1",
                "species_id": "35",
                "location_type": "network",
                "location_id": "nl",
                "period_start": "2026-03-02",
                "period_end": "2026-03-08",
                "station_id": "station-1",
                "grid_cell_id": "",
                "embedding_model": "perch_v2",
                "cluster_id": "cluster-01",
                "cluster_label": "Cluster 1",
                "cluster_rank": 1,
                "cluster_size": 1,
                "cluster_member_share": 1.0,
                "cluster_count": 1,
                "cluster_centroid_distance": 0.0,
                "cluster_distance_threshold": 0.03,
                "cluster_quality": "clustered",
                "is_cluster_exemplar": True,
                "cluster_exemplar_detection_id": "clip-1",
            }
        ]
    )
    captured: dict[str, Any] = {}

    def fake_persist_modeling_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        pipeline_module,
        "persist_modeling_run",
        fake_persist_modeling_run,
    )

    stats = _persist_outputs(
        scored,
        source_week_rows=source_week_rows,
        features=features,
        embedding_clip_rows=embedding_clip_rows,
        embedding_clusters=embedding_clusters,
        rows_loaded=10,
        pool=object(),  # type: ignore[arg-type]
        config={"min_observation_days": 3},
    )

    assert stats.run_id == captured["run_id"]
    assert stats.database_tables.startswith("modeling_runs_tbl")
    assert stats.rows_loaded == 10
    assert stats.feature_rows == 1
    assert stats.rows_scored == 1
    assert stats.alerts_red == 1
    assert stats.cluster_rows == 1
    assert stats.cluster_exemplar_rows == 1
    assert captured["run_kind"] == "anomaly_scoring"
    assert captured["status"] == "complete"
    assert captured["config"] == {"min_observation_days": 3}
    assert captured["stats"]["alerts_red"] == 1
    assert captured["features"].equals(features)
    assert captured["scores"].equals(scored)
    assert captured["clusters"].equals(embedding_clusters)
    assert captured["source_coverage"]["species_tracker"]["period_end_max"] == (
        "2026-03-08"
    )
    assert captured["source_coverage"]["features"]["period_end_max"] == "2026-03-08"
    assert captured["source_coverage"]["scored"]["period_end_max"] == "2026-03-08"
    assert captured["source_coverage"]["embedding_clips"]["detected_at_max"] == (
        "2026-03-04"
    )
    assert set(captured["source_coverage"]) == {
        "species_tracker",
        "features",
        "scored",
        "embedding_clips",
    }
    assert captured["validation_report"]["validation"]["report_version"] == (
        "weak_validation_v1"
    )
    assert captured["validation_report"]["validation"]["rows_scored"] == 1
    assert "context_diagnostics" in captured["validation_report"]["validation"]
    assert (
        "availability_stratified_metrics" in captured["validation_report"]["validation"]
    )
    assert "operational_review" not in captured["validation_report"]["validation"]


def test_persist_outputs_handles_missing_clusters(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_persist_modeling_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        pipeline_module,
        "persist_modeling_run",
        fake_persist_modeling_run,
    )

    stats = _persist_outputs(
        _scored_frame(),
        rows_loaded=1,
        pool=object(),  # type: ignore[arg-type]
        config={"include_embedding_clusters": False},
    )

    assert stats.cluster_rows == 0
    assert stats.cluster_exemplar_rows == 0
    assert captured["clusters"] is None
    assert captured["stats"]["cluster_rows"] == 0
    assert captured["config"]["include_embedding_clusters"] is False


def test_persist_outputs_can_append_window_to_existing_run(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_persist_modeling_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        pipeline_module,
        "persist_modeling_run",
        fake_persist_modeling_run,
    )

    stats = _persist_outputs(
        _scored_frame(),
        rows_loaded=5,
        pool=object(),  # type: ignore[arg-type]
        run_id="windowed-run",
        status="running",
        replace_existing_run=False,
        replace_period_start=date(2026, 3, 2),
        replace_period_end=date(2026, 3, 8),
        config={"windowed_backfill": True},
    )

    assert stats.run_id == "windowed-run"
    assert captured["run_id"] == "windowed-run"
    assert captured["status"] == "running"
    assert captured["completed_at"] is None
    assert captured["replace_existing_run"] is False
    assert captured["replace_period_start"] == date(2026, 3, 2)
    assert captured["replace_period_end"] == date(2026, 3, 8)


def test_persist_outputs_persists_validation_report(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fake_build_validation_report(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert len(args[0]) == 1
        assert kwargs["top_k"] == 25
        return {"report_version": "test_validation_v1", "rows_scored": 1}

    def fake_persist_modeling_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        pipeline_module,
        "build_validation_report",
        fake_build_validation_report,
    )
    monkeypatch.setattr(
        pipeline_module,
        "persist_modeling_run",
        fake_persist_modeling_run,
    )

    _persist_outputs(
        _scored_frame(),
        rows_loaded=1,
        pool=object(),  # type: ignore[arg-type]
        config={"include_embedding_drift": False},
    )

    assert captured["validation_report"]["validation"] == {
        "report_version": "test_validation_v1",
        "rows_scored": 1,
    }


def test_persist_outputs_can_skip_validation_report(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    def fail_build_validation_report(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise AssertionError("validation report should be skipped")

    def fake_persist_modeling_run(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        pipeline_module,
        "build_validation_report",
        fail_build_validation_report,
    )
    monkeypatch.setattr(
        pipeline_module,
        "persist_modeling_run",
        fake_persist_modeling_run,
    )

    _persist_outputs(
        _scored_frame(),
        skip_evaluation=True,
        rows_loaded=1,
        pool=object(),  # type: ignore[arg-type]
        config={"skip_evaluation": True},
    )

    assert captured["validation_report"] is None


def test_filter_period_window_limits_rows_to_target_period() -> None:
    frame = pd.DataFrame(
        [
            {"period_start": "2026-03-02", "value": "keep"},
            {"period_start": "2026-03-09", "value": "drop"},
            {"period_start": None, "value": "missing"},
        ]
    )

    filtered = _filter_period_window(
        frame,
        start_date=date(2026, 3, 2),
        end_date=date(2026, 3, 8),
    )

    assert filtered is not None
    assert filtered["value"].tolist() == ["keep"]


def test_log_weekly_frame_coverage_lists_periods(caplog) -> None:
    frame = pd.DataFrame(
        [
            {
                "species_id": "35",
                "location_id": "cell-1",
                "period_start": "2026-03-02",
                "period_end": "2026-03-08",
            },
            {
                "species_id": "36",
                "location_id": "cell-2",
                "period_start": "2026-03-09",
                "period_end": "2026-03-15",
            },
        ]
    )

    with caplog.at_level(logging.INFO):
        _log_weekly_frame_coverage(
            "modeling feature weeks to score",
            frame,
            list_periods=True,
        )

    messages = "\n".join(record.getMessage() for record in caplog.records)
    assert "weekly_periods=2" in messages
    assert "period_start=2026-03-02..2026-03-09" in messages
    assert "2026-03-02, 2026-03-09" in messages
