"""Robust seasonal population-activity baselines for weekly anomalies."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd
from numpy.typing import NDArray


_WEEKS_PER_YEAR = 52


@dataclass(frozen=True, slots=True)
class PopulationBaselineConfig:
    """Configuration for robust weekly population-activity scoring."""

    seasonal_window_weeks: int = 2
    min_location_history: int = 20
    min_seasonal_history: int = 8
    min_observation_days: int = 3
    # A1: a sharp drop in the number of contributing stations depresses
    # detections and can masquerade as a population decline. When a week's
    # active-station count falls below this fraction of the seasonal baseline
    # median, the drop signal for that row is suppressed and the row is held
    # out of the activity baseline so it cannot deflate future expectations.
    min_station_availability_ratio: float = 0.5
    min_station_baseline_history: int = 4
    # Sparse local grids often have too little same-cell seasonal history for a
    # stable baseline. Fallbacks keep the population stream informative while
    # preserving time direction: first use prior national same-species seasonal
    # rows, then a short recent local history.
    min_fallback_history: int = 2
    recent_local_history_window: int = 12
    # B3: components averaged into the population-activity index. Allows
    # ablation studies (e.g. a single ``detections_per_active_station_day``
    # component) without code changes.
    activity_components: tuple[str, ...] = (
        "relative_abundance_pct",
        "temporal_occupancy_pct",
        "station_presence_pct",
        "detections_per_active_station_day",
    )


CountBaselineConfig = PopulationBaselineConfig


@dataclass(slots=True)
class _LocationHistory:
    """Prior, time-safe history for one species/location group."""

    sufficient_history_count: int = 0
    baseline_values_by_week: dict[int, list[float]] = field(default_factory=dict)
    station_counts_by_week: dict[int, list[float]] = field(default_factory=dict)
    recent_values: list[float] = field(default_factory=list)


@dataclass(slots=True)
class _SpeciesHistory:
    """Prior national history for one species."""

    baseline_values_by_week: dict[int, list[float]] = field(default_factory=dict)


def score_population_anomalies(
    rows: pd.DataFrame,
    *,
    config: PopulationBaselineConfig | None = None,
) -> pd.DataFrame:
    """Score all species/location/week rows against seasonal population history."""
    config = config or PopulationBaselineConfig()
    frame = _normalise_input(rows, config=config)
    if frame.empty:
        return _empty_scores()

    scored = _score_rows_time_safe(frame, config)
    if scored.empty:
        return _empty_scores()
    return scored.sort_values(
        ["period_start", "population_anomaly_score"],
        ascending=[False, False],
    ).reset_index(drop=True)


def _score_rows_time_safe(
    frame: pd.DataFrame,
    config: PopulationBaselineConfig,
) -> pd.DataFrame:
    """Score rows while only exposing strictly prior weeks to baselines."""
    location_histories: dict[tuple[str, str, str], _LocationHistory] = {}
    species_histories: dict[str, _SpeciesHistory] = {}
    scored_rows: list[dict[str, Any]] = []

    ordered = frame.sort_values(
        ["period_start", "species_id", "location_type", "location_id"],
        kind="mergesort",
    )
    for _period, week_frame in ordered.groupby("period_start", sort=True):
        pending_updates: list[
            tuple[dict[str, Any], _LocationHistory, _SpeciesHistory, bool]
        ] = []
        for row in week_frame.to_dict(orient="records"):
            location_key = (
                str(row["species_id"]),
                str(row["location_type"]),
                str(row["location_id"]),
            )
            species_key = str(row["species_id"])
            location_history = location_histories.setdefault(
                location_key,
                _LocationHistory(),
            )
            species_history = species_histories.setdefault(
                species_key,
                _SpeciesHistory(),
            )
            payload, station_available = _score_row_with_history(
                row,
                location_history=location_history,
                species_history=species_history,
                config=config,
            )
            scored_rows.append(payload)
            pending_updates.append(
                (row, location_history, species_history, station_available)
            )

        for row, location_history, species_history, station_available in pending_updates:
            _update_histories(
                row,
                location_history=location_history,
                species_history=species_history,
                station_available=station_available,
                config=config,
            )

    return pd.DataFrame(scored_rows)


def _score_row_with_history(
    row: dict[str, Any],
    *,
    location_history: _LocationHistory,
    species_history: _SpeciesHistory,
    config: PopulationBaselineConfig,
) -> tuple[dict[str, Any], bool]:
    quality_reasons = _quality_reasons(row)
    baseline_level = "none"
    baseline_sample_size = 0
    expected_activity = float("nan")
    residual_z = float("nan")
    drop_score = 0.0
    spike_score = 0.0
    expected_station_count = float("nan")
    station_availability_ratio = float("nan")
    station_available = True

    week = int(row["week_of_year"])
    observed_station_count = float(row.get("community_active_station_count") or 0.0)

    station_baseline = _seasonal_baseline_values(
        location_history.station_counts_by_week or {},
        week_of_year=week,
        config=config,
    )
    if len(station_baseline) >= config.min_station_baseline_history:
        expected_station_count = float(np.median(station_baseline))
        if expected_station_count > 0:
            station_availability_ratio = observed_station_count / expected_station_count
            if station_availability_ratio < config.min_station_availability_ratio:
                station_available = False
                quality_reasons.append("station_availability_drop")

    baseline_values, baseline_level = _select_activity_baseline(
        week_of_year=week,
        location_type=str(row.get("location_type") or ""),
        location_history=location_history,
        species_history=species_history,
        config=config,
    )
    if baseline_values:
        values = np.asarray(baseline_values, dtype=float)
        observed = float(row.get("population_activity_value") or 0.0)
        expected_activity = float(np.median(values))
        scale = _robust_scale(values)
        residual_z = (observed - expected_activity) / scale if scale > 0 else 0.0
        p_at_or_below = float(np.mean(values >= observed))
        p_at_or_above = float(np.mean(values <= observed))
        drop_score = max(0.0, 2.0 * p_at_or_below - 1.0)
        spike_score = max(0.0, 2.0 * p_at_or_above - 1.0)
        baseline_sample_size = len(values)
        if not station_available:
            drop_score = 0.0
        if baseline_level != "local_seasonal":
            quality_reasons.append("fallback_baseline")
    else:
        quality_reasons.append("sparse_baseline")

    return (
        _score_payload(
            row,
            baseline_level=baseline_level,
            baseline_sample_size=baseline_sample_size,
            expected_activity=expected_activity,
            residual_z=residual_z,
            drop_score=drop_score,
            spike_score=spike_score,
            quality_reasons=quality_reasons,
            expected_station_count=expected_station_count,
            station_availability_ratio=station_availability_ratio,
        ),
        station_available,
    )


def _select_activity_baseline(
    *,
    week_of_year: int,
    location_type: str,
    location_history: _LocationHistory,
    species_history: _SpeciesHistory,
    config: PopulationBaselineConfig,
) -> tuple[list[float], str]:
    if location_history.sufficient_history_count >= config.min_location_history:
        local_seasonal = _seasonal_baseline_values(
            location_history.baseline_values_by_week or {},
            week_of_year=week_of_year,
            config=config,
        )
        if len(local_seasonal) >= config.min_seasonal_history:
            return local_seasonal, "local_seasonal"

    if location_type != "station":
        national_seasonal = _seasonal_baseline_values(
            species_history.baseline_values_by_week or {},
            week_of_year=week_of_year,
            config=config,
        )
        if len(national_seasonal) >= config.min_fallback_history:
            return national_seasonal, "species_national_seasonal"

    recent_values = list(location_history.recent_values or [])
    if len(recent_values) >= config.min_fallback_history:
        return recent_values[-config.recent_local_history_window :], "local_recent"

    return [], "none"


def _update_histories(
    row: dict[str, Any],
    *,
    location_history: _LocationHistory,
    species_history: _SpeciesHistory,
    station_available: bool,
    config: PopulationBaselineConfig,
) -> None:
    if not bool(row.get("sufficient_observation_effort", False)):
        return

    week = int(row["week_of_year"])
    observed_station_count = float(row.get("community_active_station_count") or 0.0)
    location_history.station_counts_by_week.setdefault(week, []).append(
        observed_station_count
    )
    if not station_available:
        return

    value = float(row.get("population_activity_value") or 0.0)
    location_history.baseline_values_by_week.setdefault(week, []).append(value)
    location_history.recent_values.append(value)
    if len(location_history.recent_values) > config.recent_local_history_window:
        del location_history.recent_values[
            : len(location_history.recent_values) - config.recent_local_history_window
        ]
    species_history.baseline_values_by_week.setdefault(week, []).append(value)
    location_history.sufficient_history_count += 1


def _seasonal_baseline_values(
    baseline_values_by_week: dict[int, list[float]],
    *,
    week_of_year: int,
    config: PopulationBaselineConfig,
) -> list[float]:
    return [
        value
        for candidate_week, values in baseline_values_by_week.items()
        if _week_distance(candidate_week, week_of_year) <= config.seasonal_window_weeks
        for value in values
    ]


def score_weekly_count_anomalies(
    rows: pd.DataFrame,
    *,
    config: PopulationBaselineConfig | None = None,
) -> pd.DataFrame:
    """Backward-compatible wrapper for the renamed population anomaly scorer."""
    return score_population_anomalies(rows, config=config)


def _normalise_input(
    rows: pd.DataFrame,
    *,
    config: PopulationBaselineConfig,
) -> pd.DataFrame:
    frame = rows.copy()
    if frame.empty:
        return frame

    required = {
        "species_id",
        "location_type",
        "location_id",
        "period_start",
        "total_detections",
    }
    missing = sorted(required - set(frame.columns))
    if missing:
        msg = f"Missing required modeling columns: {', '.join(missing)}"
        raise ValueError(msg)

    frame["species_id"] = frame["species_id"].astype(str)
    frame["location_type"] = frame["location_type"].astype(str)
    frame["location_id"] = frame["location_id"].astype(str)
    frame["period_start"] = pd.to_datetime(frame["period_start"]).dt.normalize()
    frame["week_of_year"] = frame["period_start"].dt.isocalendar().week.astype(int)

    if "community_active_days" not in frame.columns:
        frame["community_active_days"] = frame.get("active_days", 0.0)
    if "community_active_station_count" not in frame.columns:
        frame["community_active_station_count"] = frame.get(
            "active_station_count",
            1.0,
        )
    if "detections_per_active_station_day" not in frame.columns:
        effort = pd.to_numeric(frame["community_active_days"], errors="coerce").fillna(
            0.0
        ) * pd.to_numeric(
            frame["community_active_station_count"],
            errors="coerce",
        ).fillna(0.0)
        frame["detections_per_active_station_day"] = frame["total_detections"].div(
            effort.where(effort > 0),
        )

    for column in (
        "total_detections",
        "active_days",
        "community_active_days",
        "community_active_station_count",
        "relative_abundance_pct",
        "temporal_occupancy_pct",
        "station_presence_pct",
        "detections_per_active_station_day",
    ):
        if column not in frame.columns:
            frame[column] = 0.0
        frame[column] = pd.to_numeric(frame[column], errors="coerce").fillna(0.0)

    frame["population_activity_value"] = _population_activity_value(
        frame,
        components=config.activity_components,
    )
    frame["sufficient_observation_effort"] = (
        frame["community_active_days"] >= config.min_observation_days
    )
    if "grid_cell_id" not in frame.columns:
        frame["grid_cell_id"] = pd.NA
    return frame


_PCT_ACTIVITY_COMPONENTS = frozenset(
    {
        "relative_abundance_pct",
        "temporal_occupancy_pct",
        "station_presence_pct",
    }
)


def _population_activity_value(
    frame: pd.DataFrame,
    *,
    components: tuple[str, ...],
) -> pd.Series:
    """Combine the configured activity components into a single 0-1 index.

    Percentage components are rescaled to fractions; the detection-rate
    component is squashed with ``rate / (rate + 1)`` so it shares the same
    bounded range. With a single configured component the index is that
    component alone, which enables the B3 ablation study.
    """
    if not components:
        msg = "activity_components must contain at least one component"
        raise ValueError(msg)

    series: list[pd.Series] = []
    for name in components:
        if name in _PCT_ACTIVITY_COMPONENTS:
            series.append(_pct_fraction(frame[name]))
        elif name == "detections_per_active_station_day":
            rate = (
                pd.to_numeric(frame[name], errors="coerce").fillna(0.0).clip(lower=0.0)
            )
            series.append(rate.div(rate + 1.0))
        else:
            msg = f"Unknown activity component: {name}"
            raise ValueError(msg)

    combined = pd.concat(series, axis="columns")
    return combined.mean(axis="columns", skipna=True).fillna(0.0)


def _pct_fraction(values: pd.Series) -> pd.Series:
    return pd.to_numeric(values, errors="coerce").fillna(0.0).clip(0.0, 100.0) / 100.0


def _week_distance(left: int, right: int) -> int:
    distance = abs(left - right)
    return min(distance, _WEEKS_PER_YEAR - distance)


def _robust_scale(values: NDArray[np.float64]) -> float:
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median)))
    if mad > 0:
        return 1.4826 * mad
    std = float(np.std(values))
    return std if std > 0 else 1.0


def _quality_reasons(row: dict[str, Any]) -> list[str]:
    if bool(row.get("sufficient_observation_effort", False)):
        return ["ok"]
    return ["insufficient_observation_effort"]


def _score_payload(
    row: dict[str, Any],
    *,
    baseline_level: str,
    baseline_sample_size: int,
    expected_activity: float,
    residual_z: float,
    drop_score: float,
    spike_score: float,
    quality_reasons: list[str],
    expected_station_count: float = float("nan"),
    station_availability_ratio: float = float("nan"),
) -> dict[str, Any]:
    population_score = max(drop_score, spike_score)
    non_ok_reasons = [reason for reason in quality_reasons if reason != "ok"]
    if non_ok_reasons:
        quality_reasons = non_ok_reasons
    quality_reason = ",".join(dict.fromkeys(quality_reasons))
    return {
        "species_id": row["species_id"],
        "location_type": row["location_type"],
        "location_id": row["location_id"],
        "station_id": row.get("station_id"),
        "grid_cell_id": row.get("grid_cell_id"),
        "period_start": row["period_start"].date().isoformat(),
        "period_end": _date_iso(row.get("period_end")),
        "observed_activity": float(row.get("population_activity_value") or 0.0),
        "observed_total_detections": float(row.get("total_detections") or 0.0),
        "expected_population_activity": expected_activity,
        "population_residual_z": residual_z,
        "population_residual_abs": abs(float(residual_z))
        if pd.notna(residual_z)
        else float("nan"),
        "population_drop_score": drop_score,
        "population_spike_score": spike_score,
        "population_anomaly_score": population_score,
        "detections_per_active_station_day": float(
            row.get("detections_per_active_station_day") or 0.0
        ),
        "community_active_days": float(row.get("community_active_days") or 0.0),
        "community_active_station_count": float(
            row.get("community_active_station_count") or 0.0
        ),
        "expected_station_count": expected_station_count,
        "station_availability_ratio": station_availability_ratio,
        "sufficient_observation_effort": bool(
            row.get("sufficient_observation_effort", False)
        ),
        "quality_penalty": 0.0,
        "final_score": population_score,
        "alert_level": "green",
        "baseline_level": baseline_level,
        "baseline_sample_size": baseline_sample_size,
        "quality_mask_reason": quality_reason,
        "count_anomaly_score": population_score,
        "count_drop_score": drop_score,
        "count_spike_score": spike_score,
        "count_residual_z": residual_z,
    }


def _date_iso(value: Any) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(pd.to_datetime(value).date().isoformat())


def _empty_scores() -> pd.DataFrame:
    return pd.DataFrame(
        columns=[
            "species_id",
            "location_type",
            "location_id",
            "station_id",
            "grid_cell_id",
            "period_start",
            "period_end",
            "observed_activity",
            "observed_total_detections",
            "expected_population_activity",
            "population_residual_z",
            "population_residual_abs",
            "population_drop_score",
            "population_spike_score",
            "population_anomaly_score",
            "detections_per_active_station_day",
            "community_active_days",
            "community_active_station_count",
            "expected_station_count",
            "station_availability_ratio",
            "sufficient_observation_effort",
            "quality_penalty",
            "final_score",
            "alert_level",
            "baseline_level",
            "baseline_sample_size",
            "quality_mask_reason",
            "count_anomaly_score",
            "count_drop_score",
            "count_spike_score",
            "count_residual_z",
        ]
    )


__all__ = [
    "CountBaselineConfig",
    "PopulationBaselineConfig",
    "score_population_anomalies",
    "score_weekly_count_anomalies",
]
