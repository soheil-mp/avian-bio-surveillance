"""Environmental lag features for the bioacoustic anomaly model.

These columns are *context* for the acoustic/population anomaly signal, not
standalone alert drivers. They are continuous lagged values (no thresholds) so
that an unsupervised model can use them without importing a supervised
case-curve assumption.

Design contract:
  * Tier-1 sources (temperature, precipitation, soil moisture, NDVI) get a rich
    lag set: current (0w) and 1/3/5/7 weeks. Lagging continuous weather/vegetation
    values captures delayed ecological effects on vector/host dynamics.
  * Tier-2 sources (humidity, wind, drought SPI/SPEI) get a single 3-week lag;
    drought indices are already long-window integrals, so extra lags add little.
  * Missing source data stays NaN. We never fabricate a 0, because 0 is a
    meaningful temperature / SPI / NDVI value and a fabricated 0 would read as
    "cold/dry/bare" rather than "unknown".
"""

from __future__ import annotations

from typing import Any

import pandas as pd


# (source_column, output_prefix, aggregation) for the rich-lag Tier-1 set.
_TIER1_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("knmi_temp_mean_c", "env_temp_mean_c", "mean"),
    ("knmi_temp_max_c", "env_temp_max_c", "mean"),
    ("knmi_precip_mm", "env_precip_mm", "sum"),
    ("gee_soil_moisture_mean", "env_soil_moisture", "mean"),
    ("gee_ndvi_mean", "env_ndvi", "mean"),
)
_TIER1_LAGS: tuple[int, ...] = (0, 1, 3, 5, 7)

# (source_column, output_prefix, aggregation) for the single-lag Tier-2 set.
_TIER2_SOURCES: tuple[tuple[str, str, str], ...] = (
    ("knmi_humidity_mean_pct", "env_humidity_pct", "mean"),
    ("knmi_wind_max_ms", "env_wind_max_ms", "mean"),
    ("cds_spi_1m_mean", "env_spi_1m", "mean"),
    ("cds_spi_3m_mean", "env_spi_3m", "mean"),
    ("cds_spei_3m_mean", "env_spei_3m", "mean"),
    ("cds_spei_12m_mean", "env_spei_12m", "mean"),
)
_TIER2_LAG = 3


def _tier1_column(prefix: str, lag: int) -> str:
    return f"{prefix}_lag_{lag}w"


def _tier2_column(prefix: str) -> str:
    return f"{prefix}_lag_{_TIER2_LAG}w"


def _build_environmental_column_names() -> list[str]:
    names: list[str] = []
    for _source, prefix, _agg in _TIER1_SOURCES:
        names.extend(_tier1_column(prefix, lag) for lag in _TIER1_LAGS)
    names.extend(_tier2_column(prefix) for _source, prefix, _agg in _TIER2_SOURCES)
    names.append("environmental_has_context")
    return names


ENVIRONMENTAL_CONTEXT_COLUMNS = _build_environmental_column_names()

# The continuous environmental feature columns the anomaly model may consume
# (everything except the boolean availability flag).
ENVIRONMENTAL_FEATURE_COLUMNS = [
    name
    for name in ENVIRONMENTAL_CONTEXT_COLUMNS
    if name != "environmental_has_context"
]

_ALL_SOURCE_COLUMNS = [src for src, _p, _a in (*_TIER1_SOURCES, *_TIER2_SOURCES)]


def add_environmental_context(features: pd.DataFrame) -> pd.DataFrame:
    """Add continuous lagged weather, drought, and vegetation context columns.

    These are descriptive context for the acoustic/population anomaly signal and
    are intentionally not threshold flags; they should not create alerts on their
    own. Missing source values propagate as NaN.
    """
    frame = features.copy()
    if frame.empty:
        return _ensure_environmental_columns(frame)

    frame["period_start"] = pd.to_datetime(frame["period_start"]).dt.normalize()
    for column in _ALL_SOURCE_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")

    group_columns = ["location_type", "location_id"]
    if not set(group_columns).issubset(frame.columns):
        group_columns = []
    sort_columns = (
        [*group_columns, "period_start"] if group_columns else ["period_start"]
    )
    frame = frame.sort_values(sort_columns).copy()
    grouped = frame.groupby(group_columns, dropna=False) if group_columns else None

    for source, prefix, agg in _TIER1_SOURCES:
        for lag in _TIER1_LAGS:
            frame[_tier1_column(prefix, lag)] = _lagged_rolling(
                frame, grouped, source, weeks=max(lag, 1), agg=agg, lag_weeks=lag
            )
    for source, prefix, agg in _TIER2_SOURCES:
        frame[_tier2_column(prefix)] = _lagged_rolling(
            frame, grouped, source, weeks=_TIER2_LAG, agg=agg, lag_weeks=_TIER2_LAG
        )

    frame["environmental_has_context"] = (
        frame[_ALL_SOURCE_COLUMNS].notna().any(axis="columns")
    )
    return _ensure_environmental_columns(frame)


def _lagged_rolling(
    frame: pd.DataFrame,
    grouped: Any | None,
    column: str,
    *,
    weeks: int,
    agg: str,
    lag_weeks: int,
) -> pd.Series:
    """Rolling aggregate over a trailing window ending ``lag_weeks`` ago.

    ``lag_weeks == 0`` is the current week's value (no shift, window of 1). For
    ``lag_weeks > 0`` the value is the aggregate over the ``weeks``-long window
    ending at the lagged week, computed on data strictly before the current row
    (shifted by ``lag_weeks``) so no current-week leakage occurs.
    """

    def transform(values: pd.Series) -> pd.Series:
        numeric = pd.to_numeric(values, errors="coerce")
        if lag_weeks == 0:
            return numeric
        shifted = numeric.shift(lag_weeks)
        rolling = shifted.rolling(window=weeks, min_periods=1)
        return rolling.sum() if agg == "sum" else rolling.mean()

    if grouped is None:
        return transform(frame[column])
    return grouped[column].transform(transform)


def _ensure_environmental_columns(frame: pd.DataFrame) -> pd.DataFrame:
    for column in ENVIRONMENTAL_FEATURE_COLUMNS:
        if column not in frame.columns:
            frame[column] = pd.NA
        frame[column] = pd.to_numeric(frame[column], errors="coerce")
    if "environmental_has_context" not in frame.columns:
        frame["environmental_has_context"] = False
    frame["environmental_has_context"] = (
        frame["environmental_has_context"].astype("boolean").fillna(False).astype(bool)
    )
    return frame
