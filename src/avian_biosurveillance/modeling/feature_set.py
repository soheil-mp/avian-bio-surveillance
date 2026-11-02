"""Species-week feature-set assembly for the modeling phase."""

from __future__ import annotations

import pandas as pd

from avian_biosurveillance.config.species import registry as species_registry
from avian_biosurveillance.modeling.embedding_drift import (
    attach_embedding_weekly_features,
)
from avian_biosurveillance.modeling.environmental_context import (
    add_environmental_context,
)
from avian_biosurveillance.modeling.sovon_context import (
    attach_sovon_mortality_context,
    build_sovon_mortality_context,
)


FEATURE_SET_VERSION = "species_week_v1"


def build_species_week_feature_set(
    species_week_rows: pd.DataFrame,
    *,
    target_species_ids: tuple[str, ...] | list[str] | None = None,
    sovon_month_rows: pd.DataFrame | None = None,
    embedding_weekly_rows: pd.DataFrame | None = None,
    min_observation_days: int = 3,
) -> pd.DataFrame:
    """Build the reusable weekly feature table consumed by model streams."""
    features = _build_dense_species_week_panel(
        species_week_rows,
        target_species_ids=target_species_ids,
        min_observation_days=min_observation_days,
    )
    if features.empty:
        return features

    features["feature_set_version"] = FEATURE_SET_VERSION
    features["year"] = features["period_start"].dt.year
    features["week_of_year"] = (
        features["period_start"].dt.isocalendar().week.astype(int)
    )
    features["month_start"] = (
        features["period_start"].dt.to_period("M").dt.to_timestamp()
    )
    features["species_english"] = features["species_id"].map(
        species_registry.english_by_birdweather_id
    )
    features["sovon_species_id"] = features["species_english"].map(
        species_registry.sovon_id_by_english
    )
    features["sovon_species_key"] = features["sovon_species_id"].map(_sovon_key_from_id)
    features = add_environmental_context(features)

    sovon_context = build_sovon_mortality_context(
        sovon_month_rows if sovon_month_rows is not None else pd.DataFrame()
    )
    features = attach_sovon_mortality_context(features, sovon_context)
    features = attach_embedding_weekly_features(
        features,
        embedding_weekly_rows if embedding_weekly_rows is not None else pd.DataFrame(),
    )
    return features


def _build_dense_species_week_panel(
    rows: pd.DataFrame,
    *,
    target_species_ids: tuple[str, ...] | list[str] | None = None,
    min_observation_days: int,
) -> pd.DataFrame:
    frame = _normalise_species_week_rows(rows)
    if frame.empty or "row_kind" not in frame.columns:
        return _add_population_effort_columns(frame)

    species_rows = frame.loc[frame["row_kind"] == "species"].copy()
    community_rows = frame.loc[frame["row_kind"] == "community"].copy()
    if species_rows.empty or community_rows.empty:
        return _add_population_effort_columns(species_rows)

    community_rows = community_rows.loc[
        (community_rows["location_type"] == "grid")
        & (
            pd.to_numeric(community_rows["active_days"], errors="coerce").fillna(0)
            >= min_observation_days
        )
    ].copy()
    if community_rows.empty:
        return _add_population_effort_columns(species_rows.head(0))

    target_species = (
        [str(value) for value in target_species_ids]
        if target_species_ids is not None
        else [str(value) for value in species_registry.birdweather_ids]
    )
    base_columns = [
        "location_type",
        "location_id",
        "grid_cell_id",
        "grid_centroid_lat",
        "grid_centroid_lon",
        "grid_system",
        "period_start",
        "period_end",
        "period_day_count",
        "total_detections",
        "active_days",
        "active_station_count",
        "species_richness",
        "target_species_richness",
        "target_species_share_pct",
        "knmi_temp_mean_c",
        "knmi_temp_max_c",
        "knmi_precip_mm",
        "knmi_humidity_mean_pct",
        "knmi_wind_max_ms",
        "gee_ndvi_mean",
        "gee_soil_moisture_mean",
        "cds_spi_1m_mean",
        "cds_spi_3m_mean",
        "cds_spei_3m_mean",
        "cds_spei_12m_mean",
    ]
    for column in base_columns:
        if column not in community_rows.columns:
            community_rows[column] = pd.NA
    base = community_rows[base_columns].drop_duplicates(
        ["location_type", "location_id", "period_start"]
    )
    base = base.rename(
        columns={
            "total_detections": "community_total_detections",
            "active_days": "community_active_days",
            "active_station_count": "community_active_station_count",
        }
    )
    species_frame = pd.DataFrame({"species_id": target_species})
    dense = base.merge(species_frame, how="cross")
    merge_keys = ["species_id", "location_type", "location_id", "period_start"]
    for column in merge_keys:
        if column not in species_rows.columns:
            species_rows[column] = pd.NA

    species_merge_columns = [
        column
        for column in species_rows.columns
        if column
        not in {
            "period_end",
            "period_day_count",
            "grid_cell_id",
            "grid_system",
            "knmi_temp_mean_c",
            "knmi_temp_max_c",
            "knmi_precip_mm",
            "knmi_humidity_mean_pct",
            "knmi_wind_max_ms",
            "gee_ndvi_mean",
            "gee_soil_moisture_mean",
            "cds_spi_1m_mean",
            "cds_spi_3m_mean",
            "cds_spei_3m_mean",
            "cds_spei_12m_mean",
        }
    ]
    merged = dense.merge(
        species_rows[species_merge_columns],
        on=merge_keys,
        how="left",
        suffixes=("", "_species"),
    )
    merged["row_kind"] = "species"
    for column in (
        "total_detections",
        "active_days",
        "active_station_count",
        "species_station_count",
        "detections_per_active_day",
        "temporal_occupancy_pct",
        "relative_abundance_pct",
        "station_presence_pct",
        "high_confidence_detections",
        "high_confidence_pct",
        "processed_clip_count",
        "processed_clip_pct",
        "embedding_count",
    ):
        if column not in merged.columns:
            merged[column] = 0
        merged[column] = pd.to_numeric(merged[column], errors="coerce").fillna(0)
    return _add_population_effort_columns(merged)


def _add_population_effort_columns(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return frame
    result = frame.copy()
    if "community_active_days" not in result.columns:
        result["community_active_days"] = result.get("active_days", 0)
    if "community_active_station_count" not in result.columns:
        result["community_active_station_count"] = result.get("active_station_count", 0)
    if "community_total_detections" not in result.columns:
        result["community_total_detections"] = result.get("total_detections", 0)
    for column in (
        "community_active_days",
        "community_active_station_count",
        "community_total_detections",
        "total_detections",
    ):
        result[column] = pd.to_numeric(result[column], errors="coerce").fillna(0)
    effort = result["community_active_days"].clip(lower=0) * result[
        "community_active_station_count"
    ].clip(lower=0)
    result["detections_per_active_station_day"] = (
        result["total_detections"].div(effort.where(effort > 0)).fillna(0.0)
    )
    return result


def _normalise_species_week_rows(rows: pd.DataFrame) -> pd.DataFrame:
    frame = rows.copy()
    if frame.empty:
        return frame

    missing = {"species_id", "period_start", "period_end"} - set(frame.columns)
    if missing:
        msg = f"Missing required feature-set columns: {', '.join(sorted(missing))}"
        raise ValueError(msg)

    frame["species_id"] = frame["species_id"].astype(str)
    frame["period_start"] = pd.to_datetime(frame["period_start"]).dt.normalize()
    frame["period_end"] = pd.to_datetime(frame["period_end"]).dt.normalize()
    if "location_type" in frame.columns:
        frame["location_type"] = frame["location_type"].astype(str)
    if "location_id" in frame.columns:
        frame["location_id"] = frame["location_id"].astype(str)
    return frame


def _sovon_key_from_id(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    return f"sovon:{int(float(str(value)))}"
