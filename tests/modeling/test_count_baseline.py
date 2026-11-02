"""Tests for effort-normalized population baselines."""

from __future__ import annotations

from datetime import date

import pandas as pd

from avian_biosurveillance.modeling.count_baseline import (
    CountBaselineConfig,
    score_weekly_count_anomalies,
)


def test_scores_drop_against_seasonal_history() -> None:
    rows = pd.DataFrame(
        [
            _row(date(2022, 3, 7), total=10, relative=20.0),
            _row(date(2023, 3, 6), total=11, relative=22.0),
            _row(date(2024, 3, 4), total=9, relative=18.0),
            _row(date(2025, 3, 3), total=10, relative=20.0),
            _row(date(2026, 3, 2), total=1, relative=2.0),
        ]
    )

    scored = score_weekly_count_anomalies(rows, config=_test_config())
    result = scored.loc[scored["period_start"] == "2026-03-02"].iloc[0]

    assert len(scored) == 5
    assert result["baseline_level"] == "local_seasonal"
    assert result["baseline_sample_size"] == 4
    assert result["population_drop_score"] == 1.0
    assert result["population_anomaly_score"] == 1.0
    assert result["alert_level"] == "green"


def test_station_rows_do_not_fall_back_to_grid_history() -> None:
    rows = pd.DataFrame(
        [
            _row(date(2022, 3, 7), total=12, location_type="grid", location_id="g1"),
            _row(date(2023, 3, 6), total=11, location_type="grid", location_id="g1"),
            _row(date(2024, 3, 4), total=10, location_type="grid", location_id="g1"),
            _row(date(2025, 3, 3), total=12, location_type="grid", location_id="g1"),
            _row(
                date(2026, 3, 2),
                total=2,
                location_type="station",
                location_id="station-1",
                station_id="station-1",
            ),
        ]
    )

    scored = score_weekly_count_anomalies(rows, config=_test_config())
    result = scored.loc[scored["location_type"] == "station"].iloc[0]

    assert result["baseline_level"] == "none"
    assert result["baseline_sample_size"] == 0
    assert result["population_drop_score"] == 0.0
    assert result["quality_mask_reason"] == "sparse_baseline"


def test_minimum_observation_effort_gate_is_single_quality_penalty() -> None:
    rows = pd.DataFrame(
        [
            _row(date(2022, 3, 7), total=10),
            _row(date(2023, 3, 6), total=11),
            _row(date(2024, 3, 4), total=9),
            _row(date(2025, 3, 3), total=10),
            _row(date(2026, 3, 2), total=1, community_active_days=2),
        ]
    )

    scored = score_weekly_count_anomalies(rows, config=_test_config())
    result = scored.loc[scored["period_start"] == "2026-03-02"].iloc[0]

    assert not result["sufficient_observation_effort"]
    assert result["quality_penalty"] == 0.0
    assert result["quality_mask_reason"] == "insufficient_observation_effort"


def test_population_activity_is_stable_when_station_count_changes() -> None:
    rows = pd.DataFrame(
        [
            _row(
                date(2025, 1, 6),
                total=14,
                community_active_station_count=2,
                relative=10.0,
                occupancy=50.0,
                presence=50.0,
            ),
            _row(
                date(2025, 1, 13),
                total=28,
                community_active_station_count=4,
                relative=10.0,
                occupancy=50.0,
                presence=50.0,
            ),
        ]
    )

    scored = score_weekly_count_anomalies(
        rows,
        config=CountBaselineConfig(
            seasonal_window_weeks=0,
            min_location_history=1,
            min_seasonal_history=1,
            min_observation_days=3,
        ),
    ).sort_values("period_start")

    assert scored["detections_per_active_station_day"].tolist() == [1.0, 1.0]
    assert scored["observed_activity"].iloc[0] == scored["observed_activity"].iloc[1]


def test_station_outage_suppresses_population_drop_score() -> None:
    rows = pd.DataFrame(
        [
            _row(date(2021, 3, 8), total=40, community_active_station_count=4),
            _row(date(2022, 3, 7), total=40, community_active_station_count=4),
            _row(date(2023, 3, 6), total=40, community_active_station_count=4),
            _row(date(2024, 3, 4), total=40, community_active_station_count=4),
            _row(date(2025, 3, 3), total=40, community_active_station_count=4),
            _row(
                date(2026, 3, 2),
                total=2,
                community_active_station_count=1,
                relative=2.0,
            ),
        ]
    )

    scored = score_weekly_count_anomalies(
        rows,
        config=CountBaselineConfig(
            seasonal_window_weeks=2,
            min_location_history=3,
            min_seasonal_history=3,
            min_observation_days=3,
            min_station_availability_ratio=0.5,
            min_station_baseline_history=4,
        ),
    )
    result = scored.loc[scored["period_start"] == "2026-03-02"].iloc[0]

    assert result["station_availability_ratio"] < 0.5
    assert result["population_drop_score"] == 0.0
    assert "station_availability_drop" in result["quality_mask_reason"]


def test_sparse_grid_uses_prior_national_species_seasonal_fallback() -> None:
    rows = pd.DataFrame(
        [
            _row(date(2024, 3, 4), total=40, location_id="g1", grid_cell_id="g1"),
            _row(date(2025, 3, 3), total=44, location_id="g2", grid_cell_id="g2"),
            _row(
                date(2026, 3, 2),
                total=1,
                location_id="sparse",
                grid_cell_id="sparse",
                relative=2.0,
                occupancy=3.0,
                presence=4.0,
            ),
        ]
    )

    scored = score_weekly_count_anomalies(
        rows,
        config=CountBaselineConfig(
            seasonal_window_weeks=0,
            min_location_history=5,
            min_seasonal_history=5,
            min_fallback_history=2,
            min_observation_days=3,
        ),
    )
    result = scored.loc[scored["location_id"] == "sparse"].iloc[0]

    assert result["baseline_level"] == "species_national_seasonal"
    assert result["baseline_sample_size"] == 2
    assert result["population_anomaly_score"] > 0
    assert "fallback_baseline" in result["quality_mask_reason"]


def test_sparse_grid_uses_recent_local_fallback_when_national_season_is_missing() -> None:
    rows = pd.DataFrame(
        [
            _row(date(2026, 1, 5), total=30),
            _row(date(2026, 1, 12), total=31),
            _row(
                date(2026, 1, 19),
                total=1,
                relative=2.0,
                occupancy=3.0,
                presence=4.0,
            ),
        ]
    )

    scored = score_weekly_count_anomalies(
        rows,
        config=CountBaselineConfig(
            seasonal_window_weeks=0,
            min_location_history=5,
            min_seasonal_history=5,
            min_fallback_history=2,
            min_observation_days=3,
        ),
    )
    result = scored.loc[scored["period_start"] == "2026-01-19"].iloc[0]

    assert result["baseline_level"] == "local_recent"
    assert result["baseline_sample_size"] == 2
    assert result["population_anomaly_score"] > 0
    assert "fallback_baseline" in result["quality_mask_reason"]


def test_activity_components_can_be_restricted_to_detection_rate() -> None:
    rows = pd.DataFrame(
        [_row(date(2025, 1, 6), total=14, community_active_station_count=2)]
    )

    scored = score_weekly_count_anomalies(
        rows,
        config=CountBaselineConfig(
            seasonal_window_weeks=0,
            min_location_history=1,
            min_seasonal_history=1,
            min_observation_days=3,
            activity_components=("detections_per_active_station_day",),
        ),
    )

    # rate = 14 / (7 active days * 2 stations) = 1.0 -> 1 / (1 + 1) = 0.5
    assert scored["observed_activity"].iloc[0] == 0.5


def _test_config() -> CountBaselineConfig:
    return CountBaselineConfig(
        seasonal_window_weeks=0,
        min_location_history=3,
        min_seasonal_history=3,
        min_observation_days=3,
    )


def _row(
    period_start: date,
    *,
    total: int,
    location_type: str = "grid",
    location_id: str = "g1",
    grid_cell_id: str = "g1",
    station_id: str | None = None,
    community_active_days: int = 7,
    community_active_station_count: int = 1,
    relative: float = 20.0,
    occupancy: float = 60.0,
    presence: float = 70.0,
) -> dict[str, object]:
    return {
        "species_id": "35",
        "location_type": location_type,
        "location_id": location_id,
        "station_id": station_id,
        "grid_cell_id": grid_cell_id,
        "period_start": period_start,
        "period_end": period_start,
        "total_detections": total,
        "active_days": community_active_days,
        "active_station_count": community_active_station_count,
        "community_active_days": community_active_days,
        "community_active_station_count": community_active_station_count,
        "relative_abundance_pct": relative,
        "temporal_occupancy_pct": occupancy,
        "station_presence_pct": presence,
    }
