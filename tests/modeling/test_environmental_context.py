"""Tests for environmental modeling context features."""

from __future__ import annotations

import pandas as pd

from avian_biosurveillance.modeling.environmental_context import (
    ENVIRONMENTAL_FEATURE_COLUMNS,
    add_environmental_context,
)


def test_add_environmental_context_builds_curated_continuous_lags() -> None:
    # Eight consecutive weeks with a rising temperature ramp so each lag is
    # well-defined and checkable.
    rows = [
        _row(period_start, temp=float(index), precip=2.0, spi=0.1)
        for index, period_start in enumerate(
            pd.date_range("2026-01-05", periods=8, freq="7D").strftime("%Y-%m-%d"),
        )
    ]
    features = add_environmental_context(pd.DataFrame(rows))
    row = features.iloc[-1]  # temp == 7.0 in the final week

    # Lag 0 is the current week's raw value; lag 1 is the previous week.
    assert row["env_temp_mean_c_lag_0w"] == 7.0
    assert row["env_temp_mean_c_lag_1w"] == 6.0
    # Lag 3 averages the trailing 3-week window ending 3 weeks back: weeks with
    # temp 2, 3, 4 -> mean 3.0.
    assert row["env_temp_mean_c_lag_3w"] == 3.0
    # Precipitation aggregates by sum, not mean.
    assert row["env_precip_mm_lag_3w"] == 6.0
    # Tier-2 drought index carries a single 3-week lag column.
    assert "env_spei_3m_lag_3w" in features.columns
    assert row["environmental_has_context"]

    # No threshold/flag columns are emitted; features are continuous only.
    assert "weather_high_wind_flag" not in features.columns
    assert "weather_heavy_precip_flag" not in features.columns
    assert "drought_dry_context_flag" not in features.columns


def test_add_environmental_context_is_nan_safe_when_sources_missing() -> None:
    # Rows with no environmental sources at all must not fabricate zeros.
    rows = [
        {
            "species_id": "35",
            "location_type": "grid",
            "location_id": "c1",
            "period_start": period_start,
        }
        for period_start in pd.date_range("2026-01-05", periods=4, freq="7D").strftime(
            "%Y-%m-%d"
        )
    ]
    features = add_environmental_context(pd.DataFrame(rows))
    row = features.iloc[-1]

    assert not row["environmental_has_context"]
    for column in ENVIRONMENTAL_FEATURE_COLUMNS:
        assert pd.isna(row[column]), f"{column} should be NaN, not a fabricated value"


def test_add_environmental_context_empty_frame_has_columns() -> None:
    features = add_environmental_context(pd.DataFrame())
    for column in ENVIRONMENTAL_FEATURE_COLUMNS:
        assert column in features.columns
    assert "environmental_has_context" in features.columns


def _row(
    period_start: str,
    *,
    temp: float,
    precip: float,
    spi: float,
) -> dict[str, object]:
    return {
        "species_id": "35",
        "location_type": "network",
        "location_id": "nl",
        "period_start": period_start,
        "knmi_temp_mean_c": temp,
        "knmi_temp_max_c": temp + 3.0,
        "knmi_precip_mm": precip,
        "cds_spei_3m_mean": spi,
    }
