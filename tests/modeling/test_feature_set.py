"""Tests for species-week modeling feature-set assembly."""

from __future__ import annotations

import pandas as pd

from avian_biosurveillance.config.species import registry as species_registry
from avian_biosurveillance.modeling.feature_set import (
    FEATURE_SET_VERSION,
    build_species_week_feature_set,
)


def test_build_species_week_feature_set_attaches_sovon_context() -> None:
    features = build_species_week_feature_set(
        pd.DataFrame(
            [
                {
                    "species_id": "35",
                    "location_type": "network",
                    "location_id": "nl",
                    "period_start": "2026-01-05",
                    "period_end": "2026-01-11",
                    "total_detections": 10,
                    "active_days": 7,
                }
            ]
        ),
        sovon_month_rows=pd.DataFrame(
            [
                _sovon_row(year=2024, month=1, dead_birds=1),
                _sovon_row(year=2025, month=1, dead_birds=2),
                _sovon_row(year=2026, month=1, dead_birds=8),
            ]
        ),
    )

    row = features.iloc[0]
    assert row["feature_set_version"] == FEATURE_SET_VERSION
    assert row["species_english"] == "Eurasian Blackbird"
    assert row["sovon_species_id"] == 11870
    assert row["sovon_has_context"]
    assert row["sovon_dead_bird_count"] == 8
    assert row["sovon_mortality_score"] == 1.0


def test_build_species_week_feature_set_attaches_embedding_context() -> None:
    features = build_species_week_feature_set(
        pd.DataFrame(
            [
                {
                    "species_id": "35",
                    "location_type": "network",
                    "location_id": "nl",
                    "period_start": "2026-01-05",
                    "period_end": "2026-01-11",
                    "total_detections": 10,
                    "active_days": 7,
                }
            ]
        ),
        embedding_weekly_rows=pd.DataFrame(
            [
                {
                    "species_id": "35",
                    "location_type": "network",
                    "location_id": "nl",
                    "period_start": "2026-01-05",
                    "embedding_drift_score": 0.98,
                    "embedding_outlier_share": 0.5,
                    "embedding_clip_count_scored": 4,
                    "embedding_baseline_size": 60,
                    "embedding_baseline_level": "grid_daypart",
                    "top_detection_ids": "clip-1,clip-2",
                    "top_audio_artifact_paths": "clips/clip-1.flac",
                    "audio_quality_flag": "ok",
                }
            ]
        ),
    )

    row = features.iloc[0]
    assert row["embedding_drift_score"] == 0.98
    assert row["embedding_baseline_size"] == 60
    assert row["top_detection_ids"] == "clip-1,clip-2"


def test_build_species_week_feature_set_creates_zero_filled_dense_grid_panel() -> None:
    features = build_species_week_feature_set(
        pd.DataFrame(
            [
                {
                    "row_kind": "community",
                    "species_id": None,
                    "location_type": "grid",
                    "location_id": "g1",
                    "grid_cell_id": "g1",
                    "period_start": "2025-06-02",
                    "period_end": "2025-06-08",
                    "total_detections": 100,
                    "active_days": 4,
                    "active_station_count": 5,
                },
                {
                    "row_kind": "species",
                    "species_id": "35",
                    "location_type": "grid",
                    "location_id": "g1",
                    "grid_cell_id": "g1",
                    "period_start": "2025-06-02",
                    "period_end": "2025-06-08",
                    "total_detections": 12,
                    "active_days": 3,
                    "active_station_count": 3,
                    "relative_abundance_pct": 12.0,
                    "temporal_occupancy_pct": 75.0,
                    "station_presence_pct": 60.0,
                },
            ]
        ),
        min_observation_days=3,
    )

    assert len(features) == len(species_registry.birdweather_ids)
    detected = features.loc[features["species_id"] == "35"].iloc[0]
    assert detected["total_detections"] == 12
    assert detected["community_active_days"] == 4
    assert detected["community_active_station_count"] == 5
    assert detected["detections_per_active_station_day"] == 0.6

    zero_filled = features.loc[features["species_id"] != "35"].iloc[0]
    assert zero_filled["total_detections"] == 0
    assert zero_filled["detections_per_active_station_day"] == 0.0


def test_build_species_week_feature_set_can_limit_dense_panel_species() -> None:
    features = build_species_week_feature_set(
        pd.DataFrame(
            [
                {
                    "row_kind": "community",
                    "species_id": None,
                    "location_type": "grid",
                    "location_id": "g1",
                    "grid_cell_id": "g1",
                    "period_start": "2025-06-02",
                    "period_end": "2025-06-08",
                    "total_detections": 100,
                    "active_days": 4,
                    "active_station_count": 5,
                },
                {
                    "row_kind": "species",
                    "species_id": "35",
                    "location_type": "grid",
                    "location_id": "g1",
                    "grid_cell_id": "g1",
                    "period_start": "2025-06-02",
                    "period_end": "2025-06-08",
                    "total_detections": 12,
                    "active_days": 3,
                    "active_station_count": 3,
                },
            ]
        ),
        target_species_ids=("35",),
        min_observation_days=3,
    )

    assert features["species_id"].tolist() == ["35"]
    assert len(features) == 1


def test_build_species_week_feature_set_applies_minimum_observation_effort_gate() -> (
    None
):
    features = build_species_week_feature_set(
        pd.DataFrame(
            [
                {
                    "row_kind": "community",
                    "species_id": None,
                    "location_type": "grid",
                    "location_id": "g1",
                    "period_start": "2025-06-02",
                    "period_end": "2025-06-08",
                    "total_detections": 100,
                    "active_days": 2,
                    "active_station_count": 5,
                },
                {
                    "row_kind": "species",
                    "species_id": "35",
                    "location_type": "grid",
                    "location_id": "g1",
                    "period_start": "2025-06-02",
                    "period_end": "2025-06-08",
                    "total_detections": 12,
                    "active_days": 2,
                },
            ]
        ),
        min_observation_days=3,
    )

    assert features.empty


def test_build_species_week_feature_set_adds_environmental_lag_context() -> None:
    features = build_species_week_feature_set(
        pd.DataFrame(
            [
                _weather_row("2026-01-05", temp=5.0, precip=2.0),
                _weather_row("2026-01-12", temp=6.0, precip=3.0),
                _weather_row("2026-01-19", temp=7.0, precip=4.0),
            ]
        )
    )

    row = features.iloc[-1]
    # Current week (lag 0) is the raw value; lag 1w is the previous week.
    assert row["env_temp_mean_c_lag_0w"] == 7.0
    assert row["env_temp_mean_c_lag_1w"] == 6.0
    assert row["env_precip_mm_lag_1w"] == 3.0
    assert row["environmental_has_context"]
    assert "weather_heavy_precip_flag" not in features.columns


def _sovon_row(*, year: int, month: int, dead_birds: int) -> dict[str, object]:
    return {
        "year": year,
        "month": month,
        "species_english": "Eurasian Blackbird",
        "sovon_species_id": 11870,
        "dead_bird_count": dead_birds,
        "n_total": 10,
    }


def _weather_row(period_start: str, *, temp: float, precip: float) -> dict[str, object]:
    return {
        "species_id": "35",
        "location_type": "network",
        "location_id": "nl",
        "period_start": period_start,
        "period_end": pd.Timestamp(period_start) + pd.Timedelta(days=6),
        "total_detections": 10,
        "active_days": 7,
        "knmi_temp_mean_c": temp,
        "knmi_precip_mm": precip,
    }
