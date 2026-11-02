"""Tests for prospective (walk-forward) behavior anomaly scoring."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from avian_biosurveillance.modeling.anomaly_model import (
    BehaviorAnomalyModelConfig,
    score_behavior_anomalies_walk_forward,
)


def _multi_week_frame(n_weeks: int = 10, n_cells: int = 12) -> pd.DataFrame:
    rng = np.random.default_rng(7)
    weeks = pd.date_range("2024-01-01", periods=n_weeks, freq="7D")
    rows: list[dict[str, object]] = []
    for week in weeks:
        for cell in range(n_cells):
            rows.append(
                {
                    "species_id": "35",
                    "location_type": "grid",
                    "location_id": f"grid-{cell}",
                    "grid_cell_id": f"grid-{cell}",
                    "period_start": week,
                    "quality_mask_reason": "ok",
                    "sufficient_observation_effort": True,
                    "population_anomaly_score": float(rng.random() * 0.3),
                    "population_residual_abs": float(rng.random()),
                    "embedding_drift_score": float(rng.random() * 0.2),
                    "embedding_outlier_share": float(rng.random() * 0.2),
                    "embedding_baseline_size": int(rng.integers(50, 200)),
                    "embedding_available": True,
                }
            )
    return pd.DataFrame(rows)


def _config() -> BehaviorAnomalyModelConfig:
    return BehaviorAnomalyModelConfig(
        min_model_rows=10, amber_quantile=0.95, red_quantile=0.99
    )


def test_requires_period_start() -> None:
    frame = _multi_week_frame().drop(columns=["period_start"])
    with pytest.raises(ValueError, match="period_start"):
        score_behavior_anomalies_walk_forward(frame, config=_config())


def test_marks_scoring_mode_and_cold_start_fallback() -> None:
    scored = score_behavior_anomalies_walk_forward(
        _multi_week_frame(), config=_config()
    )
    assert set(scored["scoring_mode"]) == {"walk_forward"}
    # The earliest weeks lack enough prior history and must fall back.
    first_week = scored["period_start"].min()
    first = scored.loc[scored["period_start"] == first_week]
    assert set(first["model_fit_status"]) == {"fallback"}
    # Later weeks, once enough history accrued, get the model.
    last_week = scored["period_start"].max()
    last = scored.loc[scored["period_start"] == last_week]
    assert "isolation_forest" in set(last["model_fit_status"])


def test_no_lookahead_future_change_does_not_affect_earlier_weeks() -> None:
    # The core guarantee: a row's score depends only on data from its own week
    # and strictly earlier weeks. Perturbing a late week must leave all earlier
    # weeks' scores byte-identical.
    frame = _multi_week_frame()
    weeks = sorted(frame["period_start"].unique())
    late_week = weeks[-2]
    earlier_weeks = weeks[:-2]

    baseline = score_behavior_anomalies_walk_forward(frame, config=_config())

    perturbed = frame.copy()
    late_mask = perturbed["period_start"] == late_week
    perturbed.loc[late_mask, "embedding_drift_score"] = 0.99
    perturbed.loc[late_mask, "population_anomaly_score"] = 0.99
    perturbed_scored = score_behavior_anomalies_walk_forward(
        perturbed, config=_config()
    )

    earlier = baseline["period_start"].isin(earlier_weeks)
    pd.testing.assert_series_equal(
        baseline.loc[earlier, "model_anomaly_score"].reset_index(drop=True),
        perturbed_scored.loc[earlier, "model_anomaly_score"].reset_index(drop=True),
        check_names=False,
    )


def test_alert_tail_is_small_and_scores_bounded() -> None:
    scored = score_behavior_anomalies_walk_forward(
        _multi_week_frame(), config=_config()
    )
    assert scored["model_anomaly_score"].between(0.0, 1.0).all()
    assert not scored["model_anomaly_score"].isna().any()
    # The within-week ranking yields a small alert tail, not a flood.
    modeled = scored.loc[scored["model_fit_status"] == "isolation_forest"]
    if len(modeled):
        red_share = (modeled["alert_level"] == "red").mean()
        assert red_share <= 0.10
