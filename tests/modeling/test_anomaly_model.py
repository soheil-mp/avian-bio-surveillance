"""Tests for higher-level unsupervised behavior anomaly scoring."""

from __future__ import annotations

import pandas as pd
import pytest

from avian_biosurveillance.modeling.anomaly_model import (
    BehaviorAnomalyModelConfig,
    _alert_levels,
    score_behavior_anomalies,
)


def _behavior_rows() -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for idx in range(30):
        rows.append(
            {
                "species_id": "35",
                "location_type": "grid",
                "location_id": f"grid-{idx % 3}",
                "period_start": pd.Timestamp("2025-01-06") + pd.Timedelta(days=7 * idx),
                "quality_mask_reason": "ok",
                "population_anomaly_score": 0.05 + idx * 0.002,
                "population_drop_score": 0.0,
                "population_spike_score": 0.05 + idx * 0.001,
                "population_residual_abs": 0.1 + idx * 0.01,
                "embedding_drift_score": 0.05 + idx * 0.003,
                "embedding_outlier_share": 0.02 + idx * 0.001,
                "embedding_available": True,
            }
        )
    rows.append(
        {
            "species_id": "35",
            "location_type": "grid",
            "location_id": "grid-hot",
            "period_start": pd.Timestamp("2025-08-04"),
            "quality_mask_reason": "ok",
            "population_anomaly_score": 0.98,
            "population_drop_score": 0.98,
            "population_spike_score": 0.0,
            "population_residual_abs": 9.0,
            "embedding_drift_score": 0.96,
            "embedding_outlier_share": 0.8,
            "embedding_available": True,
        }
    )
    return pd.DataFrame(rows)


def test_score_behavior_anomalies_is_deterministic_with_fixed_seed() -> None:
    frame = _behavior_rows()
    config = BehaviorAnomalyModelConfig(
        min_model_rows=20,
        contamination=0.05,
        random_state=13,
        amber_quantile=0.90,
        red_quantile=0.98,
    )

    first = score_behavior_anomalies(frame, config=config)
    second = score_behavior_anomalies(frame, config=config)

    pd.testing.assert_series_equal(
        first["model_anomaly_score"],
        second["model_anomaly_score"],
        check_names=False,
    )
    assert first["model_feature_columns"].iloc[0]
    assert first["model_fit_status"].iloc[0] == "isolation_forest"
    assert set(first["alert_level"]) >= {"green", "amber"}


def test_score_behavior_anomalies_excludes_insufficient_effort_from_alerts() -> None:
    frame = _behavior_rows()
    frame.loc[len(frame)] = {
        "species_id": "35",
        "location_type": "grid",
        "location_id": "grid-low-effort",
        "period_start": pd.Timestamp("2025-09-01"),
        "quality_mask_reason": "insufficient_effort",
        "population_anomaly_score": 1.0,
        "population_drop_score": 1.0,
        "population_spike_score": 0.0,
        "population_residual_abs": 10.0,
        "embedding_drift_score": 1.0,
        "embedding_outlier_share": 1.0,
        "embedding_available": True,
    }

    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20,
            amber_quantile=0.90,
            red_quantile=0.98,
        ),
    )

    row = scored.loc[scored["location_id"] == "grid-low-effort"].iloc[0]
    assert not bool(row["model_eligible"])
    assert row["alert_level"] == "green"


def test_score_behavior_anomalies_falls_back_when_history_is_too_small() -> None:
    frame = _behavior_rows().head(3)

    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(min_model_rows=20),
    )

    assert set(scored["model_fit_status"]) == {"fallback"}
    assert scored["model_anomaly_score"].max() == pytest.approx(
        frame[["population_anomaly_score", "embedding_drift_score"]].max(axis=1).max()
    )


def _behavior_rows_with_environment() -> pd.DataFrame:
    """Behavior rows augmented with curated environmental columns (some NaN)."""
    frame = _behavior_rows().reset_index(drop=True)
    env_columns = [
        "env_temp_mean_c_lag_0w",
        "env_temp_mean_c_lag_3w",
        "env_precip_mm_lag_3w",
        "env_soil_moisture_lag_3w",
        "env_ndvi_lag_3w",
        "env_spi_3m_lag_3w",
        "env_spei_3m_lag_3w",
    ]
    for offset, column in enumerate(env_columns):
        frame[column] = [
            float(idx + offset) if idx % 3 else float("nan")
            for idx in range(len(frame))
        ]
    frame["environmental_has_context"] = frame["env_temp_mean_c_lag_0w"].notna()
    return frame


def test_environmental_off_uses_only_core_features() -> None:
    frame = _behavior_rows_with_environment()
    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20, include_environmental=False
        ),
    )
    columns = scored["model_feature_columns"].iloc[0].split(",")
    assert columns == [
        "population_anomaly_score",
        "population_residual_abs",
        "embedding_drift_excess_score",
        "embedding_outlier_share",
        "embedding_available",
        "embedding_coverage_score",
    ]
    assert not any(column.startswith("env_") for column in columns)


def test_bioacoustic_stream_uses_only_embedding_features() -> None:
    frame = _behavior_rows()
    frame["population_anomaly_score"] = 1.0
    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20,
            model_streams=("bioacoustic",),
        ),
    )

    columns = scored["model_feature_columns"].iloc[0].split(",")
    assert columns == [
        "embedding_drift_excess_score",
        "embedding_outlier_share",
        "embedding_available",
        "embedding_coverage_score",
    ]
    assert scored["model_streams"].iloc[0] == "bioacoustic"
    assert not any("population" in column for column in columns)


def test_population_stream_ignores_embedding_features() -> None:
    frame = _behavior_rows()
    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20,
            model_streams=("population",),
        ),
    )

    columns = scored["model_feature_columns"].iloc[0].split(",")
    assert columns == ["population_anomaly_score", "population_residual_abs"]
    assert scored["model_streams"].iloc[0] == "population"


def test_environmental_on_adds_curated_block_and_indicator() -> None:
    frame = _behavior_rows_with_environment()
    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20, include_environmental=True
        ),
    )
    columns = scored["model_feature_columns"].iloc[0].split(",")
    assert "env_temp_mean_c_lag_0w" in columns
    assert "env_spei_3m_lag_3w" in columns
    assert "environmental_available" in columns
    # Scores remain bounded despite ~1/3 of env values being NaN.
    assert scored["final_score"].between(0.0, 1.0).all()
    assert not scored["final_score"].isna().any()
    assert scored["model_fit_status"].iloc[0] == "isolation_forest"


def test_environmental_on_drops_entirely_missing_column() -> None:
    frame = _behavior_rows_with_environment()
    frame["env_temp_mean_c_lag_0w"] = float("nan")
    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20, include_environmental=True
        ),
    )
    columns = scored["model_feature_columns"].iloc[0].split(",")
    # An all-NaN environmental column carries no signal and must be excluded
    # rather than imputed to a constant.
    assert "env_temp_mean_c_lag_0w" not in columns
    # Other environmental columns with data are still present.
    assert "env_precip_mm_lag_3w" in columns


def test_environmental_handles_no_environmental_columns_gracefully() -> None:
    # When env is requested but no env columns exist at all, the model still
    # runs on the core spine plus the (all-zero) availability indicator.
    frame = _behavior_rows()
    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20, include_environmental=True
        ),
    )
    columns = scored["model_feature_columns"].iloc[0].split(",")
    assert "environmental_available" in columns
    assert scored["final_score"].between(0.0, 1.0).all()


def test_environmental_does_not_fabricate_alerts_on_quiet_rows() -> None:
    # Acoustic-led guard: rows with no biological signal (no drift, no
    # population anomaly) must not become alerts purely from environmental
    # variation. Without the guard a quiet window with varied weather produced
    # dozens of false alerts.
    import numpy as np

    rng = np.random.default_rng(0)
    n = 80
    env_columns = [
        "env_temp_mean_c_lag_0w",
        "env_temp_mean_c_lag_3w",
        "env_precip_mm_lag_3w",
        "env_soil_moisture_lag_3w",
        "env_ndvi_lag_3w",
        "env_spi_3m_lag_3w",
        "env_spei_3m_lag_3w",
    ]
    frame = pd.DataFrame(
        {
            "species_id": "35",
            "location_type": "grid",
            "location_id": [f"grid-{i}" for i in range(n)],
            "period_start": pd.Timestamp("2024-03-04"),
            "quality_mask_reason": "ok",
            "population_anomaly_score": 0.0,
            "population_residual_abs": 0.0,
            "embedding_drift_score": 0.0,
            "embedding_outlier_share": 0.0,
            "embedding_baseline_size": 0,
            "embedding_available": False,
        }
    )
    for column in env_columns:
        frame[column] = rng.normal(10.0, 5.0, n)
    frame["environmental_has_context"] = True

    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20, include_environmental=True
        ),
    )
    assert int(scored["alert_level"].isin(["amber", "red"]).sum()) == 0


def test_environmental_still_modulates_rows_with_biological_signal() -> None:
    # Where a biological signal exists, environmental features are still part of
    # the matrix and can influence the ranking.
    import numpy as np

    rng = np.random.default_rng(1)
    n = 80
    env_columns = [
        "env_temp_mean_c_lag_0w",
        "env_temp_mean_c_lag_3w",
        "env_precip_mm_lag_3w",
        "env_soil_moisture_lag_3w",
        "env_ndvi_lag_3w",
        "env_spi_3m_lag_3w",
        "env_spei_3m_lag_3w",
    ]
    frame = pd.DataFrame(
        {
            "species_id": "35",
            "location_type": "grid",
            "location_id": [f"grid-{i}" for i in range(n)],
            "period_start": pd.Timestamp("2024-03-04"),
            "quality_mask_reason": "ok",
            "population_anomaly_score": rng.random(n),
            "population_residual_abs": rng.random(n),
            "embedding_drift_score": rng.random(n),
            "embedding_outlier_share": rng.random(n),
            "embedding_baseline_size": rng.integers(50, 300, n),
            "embedding_available": True,
        }
    )
    for column in env_columns:
        frame[column] = rng.normal(10.0, 5.0, n)
    frame["environmental_has_context"] = True

    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20, include_environmental=True
        ),
    )
    columns = scored["model_feature_columns"].iloc[0].split(",")
    assert "env_temp_mean_c_lag_0w" in columns
    assert scored["model_fit_status"].iloc[0] == "isolation_forest"
    assert scored["final_score"].between(0.0, 1.0).all()


def test_alert_levels_guards_degenerate_score_distribution() -> None:
    # Reproduces the failure observed in production: an IsolationForest with no
    # usable feature variance produces near-identical rank percentiles (a spike).
    # The amber quantile then lands on the score floor, so the naive
    # ``score >= amber_threshold`` rule would flag every eligible row. The guard
    # must emit no alerts instead of alerting on everything.
    n = 200
    scores = pd.Series([0.485] * n)
    eligible = pd.Series([True] * n)

    levels = _alert_levels(
        scores, eligible=eligible, amber_quantile=0.95, red_quantile=0.99
    )

    assert set(levels) == {"green"}


def test_alert_levels_flags_top_band_on_healthy_distribution() -> None:
    # A well-separated distribution should flag roughly the intended top band --
    # not collapse to all-green, and not flag everything.
    n = 200
    scores = pd.Series([i / (n - 1) for i in range(n)])
    eligible = pd.Series([True] * n)

    levels = pd.Series(
        _alert_levels(scores, eligible=eligible, amber_quantile=0.95, red_quantile=0.99)
    )

    flagged_share = float(levels.isin(["amber", "red"]).mean())
    assert 0.0 < flagged_share <= 0.10
    assert "amber" in set(levels)
    assert "red" in set(levels)


def test_score_behavior_anomalies_excludes_signal_empty_rows_from_alerts() -> None:
    # Rows with no acoustic drift and no population residual carry no signal and
    # must not enter the model or earn alerts. Previously such rows flooded the
    # IsolationForest with identical empty vectors and collapsed the score
    # distribution into a single tie block.
    import numpy as np

    rng = np.random.default_rng(7)
    n_signal, n_empty = 28, 20
    signal = pd.DataFrame(
        {
            "species_id": "35",
            "location_type": "grid",
            "location_id": [f"sig-{i}" for i in range(n_signal)],
            "period_start": pd.Timestamp("2024-05-04"),
            "quality_mask_reason": "ok",
            "sufficient_observation_effort": True,
            "population_anomaly_score": rng.random(n_signal),
            "population_residual_abs": rng.random(n_signal),
            "embedding_drift_score": rng.random(n_signal),
            "embedding_outlier_share": rng.random(n_signal),
            "embedding_baseline_size": rng.integers(50, 300, n_signal),
            "embedding_available": True,
        }
    )
    empty = pd.DataFrame(
        {
            "species_id": "35",
            "location_type": "grid",
            "location_id": [f"empty-{i}" for i in range(n_empty)],
            "period_start": pd.Timestamp("2024-05-04"),
            "quality_mask_reason": "ok",
            "sufficient_observation_effort": True,
            "population_anomaly_score": 0.0,
            "population_residual_abs": 0.0,
            "embedding_drift_score": 0.0,
            "embedding_outlier_share": 0.0,
            "embedding_baseline_size": 0,
            "embedding_available": False,
        }
    )
    frame = pd.concat([signal, empty], ignore_index=True)

    scored = score_behavior_anomalies(
        frame,
        config=BehaviorAnomalyModelConfig(
            min_model_rows=20, amber_quantile=0.90, red_quantile=0.98
        ),
    )

    empty_rows = scored[scored["location_id"].str.startswith("empty-")]
    assert not bool(empty_rows["model_eligible"].any())
    assert set(empty_rows["alert_level"]) == {"green"}
    # Signal-bearing rows still train and are scored by the forest.
    assert "isolation_forest" in set(scored["model_fit_status"])
