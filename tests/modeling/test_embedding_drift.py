"""Tests for BirdWeather embedding drift scoring."""

from __future__ import annotations

from typing import Any

import pandas as pd

import avian_biosurveillance.modeling.embedding_drift as embedding_drift_module
from avian_biosurveillance.modeling.embedding_drift import (
    EmbeddingDriftConfig,
    attach_embedding_control,
    score_embedding_clusters,
    score_embedding_drift,
    score_embedding_scene_control,
)


def test_scores_recent_embedding_outlier_against_seasonal_history() -> None:
    rows = pd.DataFrame(
        [
            _clip("hist-1", "2023-03-06T08:00:00Z", [1.0, 0.0]),
            _clip("hist-2", "2024-03-04T08:00:00Z", [0.99, 0.01]),
            _clip("hist-3", "2025-03-03T08:00:00Z", [0.98, 0.02]),
            _clip("recent", "2026-03-02T08:00:00Z", [0.0, 1.0]),
        ]
    )

    scored = score_embedding_drift(
        rows,
        config=_test_config(),
        target_period_start="2026-03-02",
        target_period_end="2026-03-02",
    )
    grid = scored.loc[scored["location_type"] == "grid"].iloc[0]

    assert grid["embedding_drift_score"] == 1.0
    assert grid["embedding_outlier_share"] == 1.0
    assert grid["embedding_baseline_level"] == "grid_daypart"
    assert grid["embedding_baseline_size"] == 3
    assert grid["top_detection_ids"] == "recent"


def test_embedding_baseline_stays_within_same_daypart() -> None:
    rows = pd.DataFrame(
        [
            _clip("hist-1", "2023-03-06T06:00:00Z", [1.0, 0.0]),
            _clip("hist-2", "2024-03-04T06:00:00Z", [0.99, 0.01]),
            _clip("hist-3", "2025-03-03T06:00:00Z", [0.98, 0.02]),
            _clip("night-hist", "2025-03-03T23:00:00Z", [0.97, 0.03]),
            _clip("recent", "2026-03-02T06:00:00Z", [0.0, 1.0]),
        ]
    )

    scored = score_embedding_drift(
        rows,
        config=_test_config(),
        target_period_start="2026-03-02",
        target_period_end="2026-03-02",
    )
    grid = scored.loc[scored["location_type"] == "grid"].iloc[0]

    assert grid["embedding_baseline_level"] == "grid_daypart"
    assert grid["embedding_baseline_size"] == 3
    assert grid["embedding_drift_score"] == 1.0


def test_species_recommended_threshold_overrides_global_default() -> None:
    rows = pd.DataFrame(
        [
            _clip("hist-1", "2023-03-06T08:00:00Z", [1.0, 0.0]),
            _clip("hist-2", "2024-03-04T08:00:00Z", [0.99, 0.01]),
            _clip("hist-3", "2025-03-03T08:00:00Z", [0.98, 0.02]),
            _clip("recent", "2026-03-02T08:00:00Z", [0.0, 1.0], confidence=0.4),
        ]
    )

    scored = score_embedding_drift(rows, config=_test_config())

    recent = scored.loc[scored["period_start"] == "2026-03-02"].iloc[0]
    assert recent["top_detection_ids"] == "recent"


def test_noisy_background_tags_do_not_exclude_embedding_rows() -> None:
    rows = pd.DataFrame(
        [
            _clip("hist-1", "2023-03-06T08:00:00Z", [1.0, 0.0]),
            _clip("hist-2", "2024-03-04T08:00:00Z", [0.99, 0.01]),
            _clip("hist-3", "2025-03-03T08:00:00Z", [0.98, 0.02]),
            _clip(
                "recent",
                "2026-03-02T08:00:00Z",
                [0.0, 1.0],
                background_sound_clf={
                    "tags": [{"label": "Rain", "score": 0.99}],
                },
            ),
        ]
    )

    scored = score_embedding_drift(rows, config=_test_config())
    grid = scored.loc[scored["location_type"] == "grid"].iloc[0]

    assert grid["top_detection_ids"] == "recent"
    assert grid["embedding_drift_score"] == 1.0
    assert grid["background_noise_summary"] == "rain:1"


def test_score_embedding_drift_can_score_only_target_period_with_historical_baseline() -> (
    None
):
    rows = pd.DataFrame(
        [
            _clip("hist-1", "2023-03-06T08:00:00Z", [1.0, 0.0]),
            _clip("hist-2", "2024-03-04T08:00:00Z", [0.99, 0.01]),
            _clip("hist-3", "2025-03-03T08:00:00Z", [0.98, 0.02]),
            _clip("target", "2026-03-02T08:00:00Z", [0.0, 1.0]),
            _clip("future", "2026-03-09T08:00:00Z", [0.1, 0.9]),
        ]
    )

    scored = score_embedding_drift(
        rows,
        config=_test_config(),
        target_period_start="2026-03-02",
        target_period_end="2026-03-02",
    )

    assert scored["period_start"].tolist() == ["2026-03-02"]
    row = scored.iloc[0]
    assert row["embedding_baseline_size"] == 3
    assert row["top_detection_ids"] == "target"


def test_score_embedding_drift_reuses_shared_baseline_scorer(monkeypatch) -> None:
    rows = pd.DataFrame(
        [
            _clip("hist-1", "2023-03-06T08:00:00Z", [1.0, 0.0]),
            _clip("hist-2", "2024-03-04T08:00:00Z", [0.99, 0.01]),
            _clip("hist-3", "2025-03-03T08:00:00Z", [0.98, 0.02]),
            _clip("recent-a", "2026-03-02T08:00:00Z", [0.0, 1.0]),
            _clip("recent-b", "2026-03-02T09:00:00Z", [0.01, 0.99]),
        ]
    )
    calls = 0
    original = embedding_drift_module._baseline_neighbor_distances

    def counting_baseline_distances(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(
        embedding_drift_module,
        "_baseline_neighbor_distances",
        counting_baseline_distances,
    )

    scored = score_embedding_drift(
        rows,
        config=_test_config(),
        target_period_start="2026-03-02",
        target_period_end="2026-03-02",
    )

    assert len(scored) == 1
    assert calls == 1


def test_score_embedding_drift_does_not_retain_group_baseline_cache(
    monkeypatch,
) -> None:
    rows = pd.DataFrame(
        [
            _clip("g1-hist-1", "2023-03-06T08:00:00Z", [1.0, 0.0]),
            _clip("g1-hist-2", "2024-03-04T08:00:00Z", [0.99, 0.01]),
            _clip("g1-hist-3", "2025-03-03T08:00:00Z", [0.98, 0.02]),
            _clip("g1-recent", "2026-03-02T08:00:00Z", [0.0, 1.0]),
            {
                **_clip("g2-hist-1", "2023-03-06T08:00:00Z", [1.0, 0.0]),
                "grid_cell_id": "g2",
            },
            {
                **_clip("g2-hist-2", "2024-03-04T08:00:00Z", [0.99, 0.01]),
                "grid_cell_id": "g2",
            },
            {
                **_clip("g2-hist-3", "2025-03-03T08:00:00Z", [0.98, 0.02]),
                "grid_cell_id": "g2",
            },
            {
                **_clip("g2-recent", "2026-03-02T08:00:00Z", [0.01, 0.99]),
                "grid_cell_id": "g2",
            },
        ]
    )
    caches: list[object] = []
    original = embedding_drift_module._baseline_scorer

    def capturing_baseline_scorer(
        expanded: pd.DataFrame,
        row: dict[str, Any],
        config: EmbeddingDriftConfig,
        *,
        baseline_cache: Any = None,
    ) -> Any:
        caches.append(baseline_cache)
        return original(
            expanded,
            row,
            config,
            baseline_cache=baseline_cache,
        )

    monkeypatch.setattr(
        embedding_drift_module,
        "_baseline_scorer",
        capturing_baseline_scorer,
    )

    scored = score_embedding_drift(
        rows,
        config=_test_config(),
        target_period_start="2026-03-02",
        target_period_end="2026-03-02",
    )

    assert len(scored) == 2
    assert caches
    assert all(cache is None for cache in caches)


def test_normalise_input_drops_raw_embedding_vector_after_parse() -> None:
    rows = pd.DataFrame([_clip("recent", "2026-03-02T08:00:00Z", [1.0, 0.0])])

    normalised = embedding_drift_module._normalise_input(rows, config=_test_config())

    assert "embedding_array" in normalised.columns
    assert "embedding_vector" not in normalised.columns


def test_weekly_embedding_payload_reports_backend_counts() -> None:
    rows = pd.DataFrame(
        [
            _clip("hist-1", "2023-03-06T08:00:00Z", [1.0, 0.0]),
            _clip("hist-2", "2024-03-04T08:00:00Z", [0.99, 0.01]),
            _clip("hist-3", "2025-03-03T08:00:00Z", [0.98, 0.02]),
            _clip("recent-perch", "2026-03-02T08:00:00Z", [0.0, 1.0]),
            _clip(
                "recent-birdnet",
                "2026-03-02T08:05:00Z",
                [0.01, 0.99],
                embedding_model="birdnet",
            ),
        ]
    )

    scored = score_embedding_drift(
        rows,
        config=_test_config(),
        target_period_start="2026-03-02",
        target_period_end="2026-03-02",
    )
    row = scored.iloc[0]

    assert row["embedding_backend_clip_counts"] == {"perch_v2": 1, "birdnet": 1}
    assert row["embedding_backend_scored_clip_counts"] == {
        "perch_v2": 1,
        "birdnet": 1,
    }
    assert row["embedding_primary_backend"] == "perch_v2"


def test_sparse_week_still_emits_cluster_handoff_fields() -> None:
    rows = pd.DataFrame(
        [
            _clip("recent-low", "2026-03-02T08:00:00Z", [1.0, 0.0], confidence=0.82),
            _clip("recent-high", "2026-03-02T09:00:00Z", [0.99, 0.01], confidence=0.97),
        ]
    )

    scored = score_embedding_drift(rows, config=_test_config())
    grid = scored.loc[scored["location_type"] == "grid"].iloc[0]

    assert grid["embedding_drift_score"] == 0.0
    assert grid["embedding_clip_count_scored"] == 0
    assert grid["top_detection_ids"] == "recent-high,recent-low"
    assert (
        grid["top_audio_artifact_paths"]
        == "clips/recent-high.flac,clips/recent-low.flac"
    )
    assert grid["audio_quality_flag"] == "sparse_embedding_baseline"


def test_score_embedding_clusters_persists_cluster_ids_and_exemplars() -> None:
    rows = pd.DataFrame(
        [
            _clip("recent-a", "2026-03-02T08:00:00Z", [1.0, 0.0]),
            _clip("recent-b", "2026-03-02T08:05:00Z", [0.999, 0.001]),
            _clip("recent-noise", "2026-03-02T08:10:00Z", [0.0, 1.0]),
        ]
    )

    clusters = score_embedding_clusters(rows, config=_test_config())

    assert len(clusters) == 3
    clustered = clusters.loc[clusters["cluster_quality"] == "clustered"].copy()
    assert set(clustered["detection_id"]) == {"recent-a", "recent-b"}
    assert len(set(clustered["cluster_id"])) == 1
    assert clustered["cluster_size"].iloc[0] == 2
    assert clustered["cluster_count"].iloc[0] == 1
    assert clustered["cluster_exemplar_detection_id"].iloc[0] in {
        "recent-a",
        "recent-b",
    }
    assert clustered["is_cluster_exemplar"].sum() == 1

    noise = clusters.loc[clusters["detection_id"] == "recent-noise"].iloc[0]
    assert noise["cluster_quality"] == "noise"
    assert noise["cluster_label"] == "Noise"
    assert noise["cluster_count"] == 1
    assert pd.isna(noise["cluster_centroid_distance"])


def test_score_embedding_clusters_can_filter_to_target_period() -> None:
    rows = pd.DataFrame(
        [
            _clip("target-a", "2026-03-02T08:00:00Z", [1.0, 0.0]),
            _clip("target-b", "2026-03-02T08:05:00Z", [0.999, 0.001]),
            _clip("future", "2026-03-09T08:10:00Z", [0.0, 1.0]),
        ]
    )

    clusters = score_embedding_clusters(
        rows,
        config=_test_config(),
        target_period_start="2026-03-02",
        target_period_end="2026-03-02",
    )

    assert set(clusters["detection_id"]) == {"target-a", "target-b"}
    assert set(clusters["period_start"]) == {"2026-03-02"}


def test_scene_control_uses_non_target_community_median() -> None:
    weekly = pd.DataFrame(
        [
            _weekly_row("100", "cell-1", "2026-03-02", 0.9, baseline_size=10),
            _weekly_row("200", "cell-1", "2026-03-02", 0.2, baseline_size=10),
            _weekly_row("300", "cell-1", "2026-03-02", 0.4, baseline_size=10),
        ]
    )

    control = score_embedding_scene_control(
        weekly,
        target_species_ids={"100"},
        min_control_species=1,
    )

    assert len(control) == 1
    row = control.iloc[0]
    assert row["embedding_control_species_count"] == 2
    assert bool(row["embedding_control_available"])
    # Median of the two non-target species drift scores (0.2, 0.4).
    assert row["embedding_drift_control_score"] == 0.30000000000000004


def test_scene_control_marked_unavailable_below_min_species() -> None:
    weekly = pd.DataFrame(
        [
            _weekly_row("100", "cell-1", "2026-03-02", 0.9, baseline_size=10),
            _weekly_row("200", "cell-1", "2026-03-02", 0.5, baseline_size=10),
        ]
    )

    control = score_embedding_scene_control(
        weekly,
        target_species_ids={"100"},
        min_control_species=2,
    )

    row = control.iloc[0]
    assert not bool(row["embedding_control_available"])
    assert row["embedding_drift_control_score"] == 0.0


def test_attach_embedding_control_defaults_to_zero_when_missing() -> None:
    features = pd.DataFrame(
        [
            {
                "species_id": "100",
                "location_type": "grid",
                "location_id": "cell-1",
                "period_start": "2026-03-02",
            },
            {
                "species_id": "100",
                "location_type": "grid",
                "location_id": "cell-2",
                "period_start": "2026-03-02",
            },
        ]
    )
    control = pd.DataFrame(
        [
            {
                "location_type": "grid",
                "location_id": "cell-1",
                "period_start": "2026-03-02",
                "embedding_drift_control_score": 0.3,
                "embedding_control_species_count": 2,
                "embedding_control_available": True,
            }
        ]
    )

    merged = attach_embedding_control(features, control)

    cell1 = merged.loc[merged["location_id"] == "cell-1"].iloc[0]
    cell2 = merged.loc[merged["location_id"] == "cell-2"].iloc[0]
    assert cell1["embedding_drift_control_score"] == 0.3
    assert bool(cell1["embedding_control_available"])
    assert cell2["embedding_drift_control_score"] == 0.0
    assert not bool(cell2["embedding_control_available"])


def _weekly_row(
    species_id: str,
    location_id: str,
    period_start: str,
    drift_score: float,
    *,
    baseline_size: int,
) -> dict[str, object]:
    return {
        "species_id": species_id,
        "location_type": "grid",
        "location_id": location_id,
        "period_start": period_start,
        "embedding_drift_score": drift_score,
        "embedding_baseline_size": baseline_size,
    }


def _test_config() -> EmbeddingDriftConfig:
    return EmbeddingDriftConfig(
        seasonal_window_weeks=0,
        min_baseline_clips=3,
        min_week_clips=1,
        k_neighbors=2,
        min_confidence=0.7,
    )


def _clip(
    detection_id: str,
    detected_at: str,
    vector: list[float],
    *,
    station_id: str = "station-1",
    confidence: float = 0.95,
    embedding_model: str = "perch_v2",
    background_sound_clf: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "detection_id": detection_id,
        "detected_at": detected_at,
        "species_id": "35",
        "station_id": station_id,
        "grid_cell_id": "g1",
        "confidence": confidence,
        "embedding_vector": vector,
        "embedding_model": embedding_model,
        "processed_clip_path": f"clips/{detection_id}.flac",
        "background_sound_clf": background_sound_clf,
    }
