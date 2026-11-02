"""Earth Engine Profile Data Model.

Maps to the canonical grid-cell ``google_earth_engine_tbl`` plus legacy
station-profile helpers retained for compatibility.
Mirrors the flat keys emitted by
:mod:`avian_biosurveillance.ingestion.earth_engine` and preserves
legacy helpers used by the loader and tests (``SECTION_COLUMN_GROUPS``,
``build_section_availability_from_profile``,
``build_section_availability_from_row``, ``_build_residual_profile_data``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from math import hypot
from typing import Any, Mapping


# ---------------------------------------------------------------------- #
#  Small helpers                                                          #
# ---------------------------------------------------------------------- #

def _fraction_to_pct(value: Any) -> float | None:
    """Convert a cloud fraction in [0, 1] to a percentage."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and -1 <= value <= 1:
        return round(float(value) * 100, 2)
    return round(float(value), 2)


def _optional_text(value: Any) -> str | None:
    """Normalise textual sentinels to ``None``."""
    if value in {None, "", "None", "NULL", "null"}:
        return None
    return str(value)


def _as_json_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _leaf_counts(value: Any) -> tuple[int, int]:
    """Return ``(total_leaves, available_leaves)`` for a nested payload."""
    if isinstance(value, dict):
        t = a = 0
        for k, v in value.items():
            if k == "error":
                continue
            nt, na = _leaf_counts(v)
            t += nt
            a += na
        return t, a
    if isinstance(value, (list, tuple)):
        t = a = 0
        for v in value:
            nt, na = _leaf_counts(v)
            t += nt
            a += na
        return t, a
    return 1, (
        0 if value is None
        or (
            isinstance(value, str)
            and value.strip().lower() in {"", "none", "null", "nan"}
        )
        else 1
    )


def _availability_entry(
    *, total_fields: int, available_fields: int,
    status: str | None = None, error: str | None = None,
) -> dict[str, Any]:
    if status is None:
        if error is not None:
            status = "error"
        elif available_fields == 0:
            status = "missing"
        elif available_fields == total_fields:
            status = "available"
        else:
            status = "partial"
    completeness_pct = (
        round((available_fields / total_fields) * 100, 1)
        if total_fields else 0.0
    )
    entry: dict[str, Any] = {
        "status": status,
        "available_fields": available_fields,
        "missing_fields": max(total_fields - available_fields, 0),
        "total_fields": total_fields,
        "completeness_pct": completeness_pct,
    }
    if error is not None:
        entry["error"] = error
    return entry


# ---------------------------------------------------------------------- #
#  Flat → DB column mapping, per section.                                 #
# ---------------------------------------------------------------------- #

SECTION_COLUMN_GROUPS: dict[str, tuple[str, ...]] = {
    "elevation": (
        "elevation_m", "slope_degrees", "aspect_degrees",
    ),
    "land_cover": (
        "land_cover_class", "dynamic_world_label",
        "esa_worldcover_class", "esa_worldcover_label",
        "modis_igbp_class", "modis_igbp_label",
        "dw_water_prob", "dw_trees_prob", "dw_grass_prob",
        "dw_flooded_vegetation_prob", "dw_crops_prob",
        "dw_shrub_and_scrub_prob",
        "dw_built_prob", "dw_bare_prob", "dw_snow_and_ice_prob",
        "cop_lc_tree_pct", "cop_lc_shrub_pct", "cop_lc_grass_pct",
        "cop_lc_crops_pct", "cop_lc_urban_pct", "cop_lc_bare_pct",
        "cop_lc_water_permanent_pct", "cop_lc_water_seasonal_pct",
        "cop_lc_moss_lichen_pct", "cop_lc_snow_ice_pct",
    ),
    "vegetation": (
        "ndvi", "evi", "lai", "fpar", "ndvi_summary_qa",
    ),
    "surface_temperature": (
        "lst_day_celsius", "lst_night_celsius",
        "lst_qc_day", "lst_qc_night",
    ),
    "climate": (
        "temperature_2m_c", "dewpoint_2m_c", "skin_temperature_c",
        "total_precipitation_mm", "snowfall_m", "snow_depth_m",
        "era5_snow_cover", "era5_snow_density", "era5_snowmelt_m",
        "era5_snow_albedo",
        "wind_speed_10m_ms", "wind_u_10m_ms", "wind_v_10m_ms",
        "surface_pressure_pa",
        "soil_temp_level1_c", "soil_temp_level2_c",
        "soil_temp_level3_c", "soil_temp_level4_c",
        "soil_moisture", "soil_moisture_level2",
        "soil_moisture_level3", "soil_moisture_level4",
        "surface_solar_rad_down_j_m2", "surface_thermal_rad_down_j_m2",
        "surface_net_solar_rad_j_m2", "surface_net_thermal_rad_j_m2",
        "surface_latent_heat_j_m2", "surface_sensible_heat_j_m2",
        "total_evaporation_m", "potential_evaporation_m",
        "evap_bare_soil_m", "evap_open_water_m",
        "evap_canopy_m", "evap_vegetation_m",
        "runoff_m", "surface_runoff_m", "sub_surface_runoff_m",
        "leaf_area_index_high", "leaf_area_index_low",
    ),
    "air_quality": (
        "no2_mol_m2", "co_mol_m2", "so2_mol_m2", "ozone_mol_m2",
        "aerosol_index", "ch4_ppb", "hcho_mol_m2",
    ),
    "cloud_properties": (
        "cloud_cover_pct", "cloud_top_height_m",
        "cloud_base_height_m", "cloud_optical_depth",
    ),
    "surface_water": (
        "surface_water_pct", "surface_water_change_abs",
        "surface_water_seasonality_months", "surface_water_recurrence_pct",
        "surface_water_max_extent", "surface_water_transition",
    ),
    "forest": (
        "forest_cover_pct", "forest_loss", "forest_gain",
        "forest_loss_year",
    ),
    "nighttime_lights": (
        "nighttime_radiance_nw", "cloud_free_observations",
    ),
    "population": (
        "worldpop_count", "gpw_density_per_km2", "population_source_year",
    ),
    "human_modification": (
        "human_modification_index",
    ),
    "fire": (
        "burned_area_date", "fire_brightness_kelvin",
        "fire_frp_mw", "fire_confidence",
    ),
    "soil": (
        "clay_pct", "sand_pct", "organic_carbon_g_kg",
        "soil_ph", "bulk_density_kg_m3",
    ),
    "evapotranspiration": (
        "et_mm_8day", "pet_mm_8day",
        "latent_heat_le_j_m2_day", "potential_le_j_m2_day", "et_qc",
    ),
    "sea_surface_temperature": (
        "sst_celsius", "sst_anom_celsius",
    ),
    "sentinel2": (
        "s2_ndvi", "s2_ndwi", "s2_mndwi", "s2_ndmi",
        "s2_evi", "s2_nbr", "s2_image_count",
    ),
    "sentinel1": (
        "s1_vv_db", "s1_vh_db", "s1_vv_vh_ratio_db", "s1_image_count",
    ),
    "chirps": (
        "chirps_precip_mm",
    ),
    "modis_snow": (
        "modis_snow_cover_pct", "modis_ndsi",
    ),
    "modis_productivity": (
        "modis_gpp_kg_c_m2", "modis_psn_net_kg_c_m2",
    ),
    "phenology": (
        "phenology_greenup_doy", "phenology_midgreenup_doy",
        "phenology_peak_doy", "phenology_maturity_doy",
        "phenology_senescence_doy", "phenology_dormancy_doy",
        "phenology_evi_amplitude", "phenology_evi_area",
        "phenology_qa",
    ),
    "black_marble": (
        "black_marble_ntl_nw", "black_marble_qa",
    ),
}


_ALL_METRIC_COLUMNS: tuple[str, ...] = tuple(
    c for cols in SECTION_COLUMN_GROUPS.values() for c in cols
)


# ---------------------------------------------------------------------- #
#  Section-availability builders                                          #
# ---------------------------------------------------------------------- #

def build_section_availability_from_profile(
    profile: Mapping[str, Any],
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for section_name, columns in SECTION_COLUMN_GROUPS.items():
        if section_name not in profile:
            out[section_name] = _availability_entry(
                total_fields=len(columns), available_fields=0,
                status="not_queried",
            )
            continue
        value = profile.get(section_name)
        error = None
        if isinstance(value, dict) and value.get("error") is not None:
            error = str(value["error"])
        total, available = _leaf_counts(value)
        if total == 0:
            total = len(columns)
        out[section_name] = _availability_entry(
            total_fields=total, available_fields=available, error=error,
        )
    return out


def build_section_availability_from_row(
    row: Mapping[str, Any],
) -> dict[str, Any]:
    profile_data = _as_json_dict(row.get("profile_data"))
    out: dict[str, Any] = {}
    for section_name, columns in SECTION_COLUMN_GROUPS.items():
        residual = profile_data.get(section_name)
        if residual is None:
            rtotal = ravail = 0
        else:
            rtotal, ravail = _leaf_counts(residual)
        error = None
        if isinstance(residual, dict) and residual.get("error") is not None:
            error = str(residual["error"])
        mapped_total = len(columns)
        mapped_available = sum(row.get(c) is not None for c in columns)
        out[section_name] = _availability_entry(
            total_fields=mapped_total + rtotal,
            available_fields=mapped_available + ravail,
            error=error,
        )
    return out


# ---------------------------------------------------------------------- #
#  Residual (JSONB overflow) builder                                      #
# ---------------------------------------------------------------------- #

def _build_residual_profile_data(
    profile: dict[str, Any],
    *,
    extra_top_level_mapped: set[str] | None = None,
) -> str:
    """Serialise the portion of ``profile`` not covered by flat columns."""
    mapped_flat = set(_ALL_METRIC_COLUMNS)
    nested_fully_mapped = {
        "land_cover": {
            "dynamic_world": {"value", "label"},
            "esa_worldcover": {"value", "label"},
            "modis_igbp": {"value", "label"},
        },
    }
    # Keys within each section that are consumed by `_build_common_row_data`
    # (mapped to a different flat column) and should NOT appear in residual.
    consumed_section_keys: dict[str, set[str]] = {
        "location": {"latitude", "longitude", "lat", "lon", "date"},
        "cloud_properties": {"cloud_fraction"},
    }

    residual: dict[str, Any] = {}
    skip_top_level = {
        "station_id", "grid_cell_id", "source", "lat", "lon", "date",
        "section_availability", "profile_data",
        "bbox_min_lat", "bbox_min_lon", "bbox_max_lat", "bbox_max_lon",
        "grid_size_km",
    }
    for section, value in profile.items():
        if section in skip_top_level:
            continue
        if extra_top_level_mapped and section in extra_top_level_mapped:
            continue
        if section == "_meta":
            if isinstance(value, dict):
                extras = {
                    k: v for k, v in value.items()
                    if k not in {"mode", "total_seconds"} and v is not None
                }
                if extras:
                    residual["_meta"] = extras
            continue
        if not isinstance(value, dict):
            residual[section] = value
            continue

        sub_residual: dict[str, Any] = {}
        if value.get("error") is not None:
            sub_residual["error"] = value["error"]

        nested_mapped = nested_fully_mapped.get(section, {})
        consumed = consumed_section_keys.get(section, set())
        for k, v in value.items():
            if k == "error":
                continue
            if k in consumed:
                continue
            if isinstance(v, dict) and k in nested_mapped:
                extra = {
                    nk: nv for nk, nv in v.items()
                    if nk not in nested_mapped[k] and nv is not None
                }
                if extra:
                    sub_residual[k] = extra
                continue
            if k in mapped_flat and not isinstance(v, dict):
                continue
            if v is None:
                continue
            sub_residual[k] = v

        if sub_residual:
            residual[section] = sub_residual

    return json.dumps(residual or {}, default=str)


# ---------------------------------------------------------------------- #
#  Flat-row builder                                                       #
# ---------------------------------------------------------------------- #

def _section(profile: dict[str, Any], name: str) -> dict[str, Any]:
    val = profile.get(name)
    return val if isinstance(val, dict) else {}


def _build_common_row_data(
    profile: dict[str, Any],
    *,
    observation_date: date,
    source: str | None = None,
    lat: float | None = None,
    lon: float | None = None,
    extra_top_level_mapped: set[str] | None = None,
) -> dict[str, Any]:
    """Flatten an Earth Engine profile dict into dataclass-ready kwargs."""
    land_cover = _section(profile, "land_cover")
    climate = _section(profile, "climate")
    cloud = _section(profile, "cloud_properties")

    dw = land_cover.get("dynamic_world") if isinstance(
        land_cover, dict) else None
    esa = land_cover.get("esa_worldcover") if isinstance(
        land_cover, dict) else None
    modis = land_cover.get("modis_igbp") if isinstance(
        land_cover, dict) else None

    lc_class = dw.get("value") if isinstance(dw, dict) else None
    lc_label = _optional_text(
        dw.get("label") if isinstance(dw, dict) else None)
    esa_class = esa.get("value") if isinstance(esa, dict) else None
    esa_label = _optional_text(
        esa.get("label") if isinstance(esa, dict) else None)
    modis_class = modis.get("value") if isinstance(modis, dict) else None
    modis_label = _optional_text(
        modis.get("label") if isinstance(modis, dict) else None,
    )

    wu = climate.get("wind_u_10m_ms")
    wv = climate.get("wind_v_10m_ms")
    wind_speed = (
        round(hypot(wu, wv), 2) if wu is not None and wv is not None else None
    )

    cloud_cover_pct = cloud.get("cloud_cover_pct")
    if cloud_cover_pct is None and cloud.get("cloud_fraction") is not None:
        cloud_cover_pct = _fraction_to_pct(cloud.get("cloud_fraction"))

    location = _section(profile, "location")
    resolved_lat = lat if lat is not None else (
        profile.get("lat") or location.get("latitude") or location.get("lat")
    )
    resolved_lon = lon if lon is not None else (
        profile.get("lon") or location.get("longitude") or location.get("lon")
    )

    row: dict[str, Any] = {
        "observation_date": observation_date,
        "lat": resolved_lat,
        "lon": resolved_lon,
        "source": source,
    }
    for section_name in SECTION_COLUMN_GROUPS:
        section_value = _section(profile, section_name)
        for col in SECTION_COLUMN_GROUPS[section_name]:
            if col in row:
                continue
            row[col] = section_value.get(col)

    row["land_cover_class"] = lc_class
    row["dynamic_world_label"] = lc_label
    row["esa_worldcover_class"] = esa_class
    row["esa_worldcover_label"] = esa_label
    row["modis_igbp_class"] = modis_class
    row["modis_igbp_label"] = modis_label
    row["wind_speed_10m_ms"] = wind_speed
    row["cloud_cover_pct"] = cloud_cover_pct

    meta = _section(profile, "_meta")
    row["profiler_mode"] = meta.get("mode")
    row["profiler_elapsed_s"] = meta.get("total_seconds")

    row["profile_data"] = _build_residual_profile_data(
        profile,
        extra_top_level_mapped=extra_top_level_mapped,
    )
    availability = profile.get("section_availability")
    if availability is None:
        availability = build_section_availability_from_profile(profile)
    row["section_availability"] = (
        availability if isinstance(availability, str)
        else json.dumps(availability, default=str)
    )
    return row


# ---------------------------------------------------------------------- #
#  Dataclasses                                                            #
# ---------------------------------------------------------------------- #

@dataclass(frozen=True, slots=True)
class EarthEngineProfile:
    """One satellite-derived profile per station per date."""

    station_id: str
    observation_date: date
    lat: float
    lon: float
    source: str | None = None

    elevation_m: float | None = None
    slope_degrees: float | None = None
    aspect_degrees: float | None = None

    ndvi: float | None = None
    evi: float | None = None
    lai: float | None = None
    fpar: float | None = None
    ndvi_summary_qa: int | None = None

    lst_day_celsius: float | None = None
    lst_night_celsius: float | None = None
    lst_qc_day: int | None = None
    lst_qc_night: int | None = None

    temperature_2m_c: float | None = None
    dewpoint_2m_c: float | None = None
    skin_temperature_c: float | None = None
    total_precipitation_mm: float | None = None
    snowfall_m: float | None = None
    snow_depth_m: float | None = None
    era5_snow_cover: float | None = None
    era5_snow_density: float | None = None
    era5_snowmelt_m: float | None = None
    era5_snow_albedo: float | None = None
    wind_speed_10m_ms: float | None = None
    wind_u_10m_ms: float | None = None
    wind_v_10m_ms: float | None = None
    surface_pressure_pa: float | None = None
    soil_temp_level1_c: float | None = None
    soil_temp_level2_c: float | None = None
    soil_temp_level3_c: float | None = None
    soil_temp_level4_c: float | None = None
    soil_moisture: float | None = None
    soil_moisture_level2: float | None = None
    soil_moisture_level3: float | None = None
    soil_moisture_level4: float | None = None
    surface_solar_rad_down_j_m2: float | None = None
    surface_thermal_rad_down_j_m2: float | None = None
    surface_net_solar_rad_j_m2: float | None = None
    surface_net_thermal_rad_j_m2: float | None = None
    surface_latent_heat_j_m2: float | None = None
    surface_sensible_heat_j_m2: float | None = None
    total_evaporation_m: float | None = None
    potential_evaporation_m: float | None = None
    evap_bare_soil_m: float | None = None
    evap_open_water_m: float | None = None
    evap_canopy_m: float | None = None
    evap_vegetation_m: float | None = None
    runoff_m: float | None = None
    surface_runoff_m: float | None = None
    sub_surface_runoff_m: float | None = None
    leaf_area_index_high: float | None = None
    leaf_area_index_low: float | None = None

    cloud_cover_pct: float | None = None
    cloud_top_height_m: float | None = None
    cloud_base_height_m: float | None = None
    cloud_optical_depth: float | None = None

    no2_mol_m2: float | None = None
    co_mol_m2: float | None = None
    so2_mol_m2: float | None = None
    ozone_mol_m2: float | None = None
    aerosol_index: float | None = None
    ch4_ppb: float | None = None
    hcho_mol_m2: float | None = None

    land_cover_class: int | None = None
    dynamic_world_label: str | None = None
    esa_worldcover_class: int | None = None
    esa_worldcover_label: str | None = None
    modis_igbp_class: int | None = None
    modis_igbp_label: str | None = None

    # Dynamic World probabilities (prefixed with ``dw_`` to match DB).
    dw_water_prob: float | None = None
    dw_trees_prob: float | None = None
    dw_grass_prob: float | None = None
    dw_flooded_vegetation_prob: float | None = None
    dw_crops_prob: float | None = None
    dw_shrub_and_scrub_prob: float | None = None
    dw_built_prob: float | None = None
    dw_bare_prob: float | None = None
    dw_snow_and_ice_prob: float | None = None

    # Copernicus LC 100 m cover fractions
    cop_lc_tree_pct: float | None = None
    cop_lc_shrub_pct: float | None = None
    cop_lc_grass_pct: float | None = None
    cop_lc_crops_pct: float | None = None
    cop_lc_urban_pct: float | None = None
    cop_lc_bare_pct: float | None = None
    cop_lc_water_permanent_pct: float | None = None
    cop_lc_water_seasonal_pct: float | None = None
    cop_lc_moss_lichen_pct: float | None = None
    cop_lc_snow_ice_pct: float | None = None

    forest_cover_pct: float | None = None
    forest_loss: bool | None = None
    forest_gain: bool | None = None
    forest_loss_year: int | None = None

    surface_water_pct: float | None = None
    surface_water_change_abs: float | None = None
    surface_water_seasonality_months: float | None = None
    surface_water_recurrence_pct: float | None = None
    surface_water_max_extent: int | None = None
    surface_water_transition: int | None = None

    nighttime_radiance_nw: float | None = None
    cloud_free_observations: int | None = None

    worldpop_count: float | None = None
    gpw_density_per_km2: float | None = None
    population_source_year: int | None = None

    human_modification_index: float | None = None

    burned_area_date: int | None = None
    fire_brightness_kelvin: float | None = None
    fire_frp_mw: float | None = None
    fire_confidence: int | None = None

    clay_pct: float | None = None
    sand_pct: float | None = None
    organic_carbon_g_kg: float | None = None
    soil_ph: float | None = None
    bulk_density_kg_m3: float | None = None

    et_mm_8day: float | None = None
    pet_mm_8day: float | None = None
    latent_heat_le_j_m2_day: float | None = None
    potential_le_j_m2_day: float | None = None
    et_qc: int | None = None

    sst_celsius: float | None = None
    sst_anom_celsius: float | None = None

    s2_ndvi: float | None = None
    s2_ndwi: float | None = None
    s2_mndwi: float | None = None
    s2_ndmi: float | None = None
    s2_evi: float | None = None
    s2_nbr: float | None = None
    s2_image_count: int | None = None

    s1_vv_db: float | None = None
    s1_vh_db: float | None = None
    s1_vv_vh_ratio_db: float | None = None
    s1_image_count: int | None = None

    chirps_precip_mm: float | None = None

    modis_snow_cover_pct: float | None = None
    modis_ndsi: float | None = None
    modis_gpp_kg_c_m2: float | None = None
    modis_psn_net_kg_c_m2: float | None = None

    phenology_greenup_doy: int | None = None
    phenology_midgreenup_doy: int | None = None
    phenology_peak_doy: int | None = None
    phenology_maturity_doy: int | None = None
    phenology_senescence_doy: int | None = None
    phenology_dormancy_doy: int | None = None
    phenology_evi_amplitude: float | None = None
    phenology_evi_area: float | None = None
    phenology_qa: int | None = None

    black_marble_ntl_nw: float | None = None
    black_marble_qa: int | None = None

    profile_data: str | None = None
    section_availability: str | None = None

    profiler_mode: str | None = None
    profiler_elapsed_s: float | None = None

    @classmethod
    def from_profile_dict(
        cls,
        profile: dict[str, Any],
        *,
        station_id: str,
        observation_date: date,
        source: str | None = None,
    ) -> EarthEngineProfile:
        row = _build_common_row_data(
            profile,
            observation_date=observation_date,
            source=source,
        )
        row["station_id"] = station_id
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in valid})

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def column_names() -> list[str]:
        return [f.name for f in fields(EarthEngineProfile)]

    @staticmethod
    def pandas_dtypes() -> dict[str, str]:
        int_cols = {
            "land_cover_class", "esa_worldcover_class", "modis_igbp_class",
            "ndvi_summary_qa", "lst_qc_day", "lst_qc_night",
            "forest_loss_year", "surface_water_max_extent",
            "surface_water_transition", "cloud_free_observations",
            "population_source_year", "burned_area_date",
            "fire_confidence", "et_qc",
            "s2_image_count", "s1_image_count",
            "phenology_greenup_doy", "phenology_midgreenup_doy",
            "phenology_peak_doy", "phenology_maturity_doy",
            "phenology_senescence_doy", "phenology_dormancy_doy",
            "phenology_qa", "black_marble_qa",
        }
        string_cols = {
            "station_id", "source", "dynamic_world_label",
            "esa_worldcover_label", "modis_igbp_label", "profiler_mode",
            "profile_data", "section_availability",
        }
        bool_cols = {"forest_loss", "forest_gain"}
        date_cols = {"observation_date"}
        out: dict[str, str] = {}
        for f in fields(EarthEngineProfile):
            if f.name in int_cols:
                out[f.name] = "Int64"
            elif f.name in string_cols:
                out[f.name] = "string"
            elif f.name in bool_cols:
                out[f.name] = "boolean"
            elif f.name in date_cols:
                out[f.name] = "object"
            else:
                out[f.name] = "Float64"
        return out


@dataclass(frozen=True, slots=True)
class EarthEngineGridProfile:
    """Environmental features for one grid cell per date."""

    grid_cell_id: str
    observation_date: date
    source: str | None = None

    elevation_m: float | None = None
    slope_degrees: float | None = None
    aspect_degrees: float | None = None

    ndvi: float | None = None
    evi: float | None = None
    lai: float | None = None
    fpar: float | None = None
    ndvi_summary_qa: int | None = None

    lst_day_celsius: float | None = None
    lst_night_celsius: float | None = None
    lst_qc_day: int | None = None
    lst_qc_night: int | None = None

    temperature_2m_c: float | None = None
    dewpoint_2m_c: float | None = None
    skin_temperature_c: float | None = None
    total_precipitation_mm: float | None = None
    snowfall_m: float | None = None
    snow_depth_m: float | None = None
    era5_snow_cover: float | None = None
    era5_snow_density: float | None = None
    era5_snowmelt_m: float | None = None
    era5_snow_albedo: float | None = None
    wind_speed_10m_ms: float | None = None
    wind_u_10m_ms: float | None = None
    wind_v_10m_ms: float | None = None
    surface_pressure_pa: float | None = None
    soil_temp_level1_c: float | None = None
    soil_temp_level2_c: float | None = None
    soil_temp_level3_c: float | None = None
    soil_temp_level4_c: float | None = None
    soil_moisture: float | None = None
    soil_moisture_level2: float | None = None
    soil_moisture_level3: float | None = None
    soil_moisture_level4: float | None = None
    surface_solar_rad_down_j_m2: float | None = None
    surface_thermal_rad_down_j_m2: float | None = None
    surface_net_solar_rad_j_m2: float | None = None
    surface_net_thermal_rad_j_m2: float | None = None
    surface_latent_heat_j_m2: float | None = None
    surface_sensible_heat_j_m2: float | None = None
    total_evaporation_m: float | None = None
    potential_evaporation_m: float | None = None
    evap_bare_soil_m: float | None = None
    evap_open_water_m: float | None = None
    evap_canopy_m: float | None = None
    evap_vegetation_m: float | None = None
    runoff_m: float | None = None
    surface_runoff_m: float | None = None
    sub_surface_runoff_m: float | None = None
    leaf_area_index_high: float | None = None
    leaf_area_index_low: float | None = None

    cloud_cover_pct: float | None = None
    cloud_top_height_m: float | None = None
    cloud_base_height_m: float | None = None
    cloud_optical_depth: float | None = None

    no2_mol_m2: float | None = None
    co_mol_m2: float | None = None
    so2_mol_m2: float | None = None
    ozone_mol_m2: float | None = None
    aerosol_index: float | None = None
    ch4_ppb: float | None = None
    hcho_mol_m2: float | None = None

    land_cover_class: int | None = None
    dynamic_world_label: str | None = None
    esa_worldcover_class: int | None = None
    esa_worldcover_label: str | None = None
    modis_igbp_class: int | None = None
    modis_igbp_label: str | None = None

    dw_water_prob: float | None = None
    dw_trees_prob: float | None = None
    dw_grass_prob: float | None = None
    dw_flooded_vegetation_prob: float | None = None
    dw_crops_prob: float | None = None
    dw_shrub_and_scrub_prob: float | None = None
    dw_built_prob: float | None = None
    dw_bare_prob: float | None = None
    dw_snow_and_ice_prob: float | None = None

    cop_lc_tree_pct: float | None = None
    cop_lc_shrub_pct: float | None = None
    cop_lc_grass_pct: float | None = None
    cop_lc_crops_pct: float | None = None
    cop_lc_urban_pct: float | None = None
    cop_lc_bare_pct: float | None = None
    cop_lc_water_permanent_pct: float | None = None
    cop_lc_water_seasonal_pct: float | None = None
    cop_lc_moss_lichen_pct: float | None = None
    cop_lc_snow_ice_pct: float | None = None

    forest_cover_pct: float | None = None
    forest_loss: bool | None = None
    forest_gain: bool | None = None
    forest_loss_year: int | None = None

    surface_water_pct: float | None = None
    surface_water_change_abs: float | None = None
    surface_water_seasonality_months: float | None = None
    surface_water_recurrence_pct: float | None = None
    surface_water_max_extent: int | None = None
    surface_water_transition: int | None = None

    nighttime_radiance_nw: float | None = None
    cloud_free_observations: int | None = None

    worldpop_count: float | None = None
    gpw_density_per_km2: float | None = None
    population_source_year: int | None = None

    human_modification_index: float | None = None

    burned_area_date: int | None = None
    fire_brightness_kelvin: float | None = None
    fire_frp_mw: float | None = None
    fire_confidence: int | None = None

    clay_pct: float | None = None
    sand_pct: float | None = None
    organic_carbon_g_kg: float | None = None
    soil_ph: float | None = None
    bulk_density_kg_m3: float | None = None

    et_mm_8day: float | None = None
    pet_mm_8day: float | None = None
    latent_heat_le_j_m2_day: float | None = None
    potential_le_j_m2_day: float | None = None
    et_qc: int | None = None

    sst_celsius: float | None = None
    sst_anom_celsius: float | None = None

    s2_ndvi: float | None = None
    s2_ndwi: float | None = None
    s2_mndwi: float | None = None
    s2_ndmi: float | None = None
    s2_evi: float | None = None
    s2_nbr: float | None = None
    s2_image_count: int | None = None

    s1_vv_db: float | None = None
    s1_vh_db: float | None = None
    s1_vv_vh_ratio_db: float | None = None
    s1_image_count: int | None = None

    chirps_precip_mm: float | None = None

    modis_snow_cover_pct: float | None = None
    modis_ndsi: float | None = None
    modis_gpp_kg_c_m2: float | None = None
    modis_psn_net_kg_c_m2: float | None = None

    phenology_greenup_doy: int | None = None
    phenology_midgreenup_doy: int | None = None
    phenology_peak_doy: int | None = None
    phenology_maturity_doy: int | None = None
    phenology_senescence_doy: int | None = None
    phenology_dormancy_doy: int | None = None
    phenology_evi_amplitude: float | None = None
    phenology_evi_area: float | None = None
    phenology_qa: int | None = None

    black_marble_ntl_nw: float | None = None
    black_marble_qa: int | None = None

    profile_data: str | None = None
    section_availability: str | None = None

    profiler_mode: str | None = None
    profiler_elapsed_s: float | None = None

    @classmethod
    def from_profile_dict(
        cls,
        profile: dict[str, Any],
        *,
        grid_cell_id: str,
        observation_date: date,
        source: str | None = None,
    ) -> EarthEngineGridProfile:
        row = _build_common_row_data(
            profile,
            observation_date=observation_date,
            source=source,
            extra_top_level_mapped={"grid_cell_id"},
        )
        row.pop("lat", None)
        row.pop("lon", None)
        row["grid_cell_id"] = grid_cell_id
        valid = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in row.items() if k in valid})

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def column_names() -> list[str]:
        return [f.name for f in fields(EarthEngineGridProfile)]

    @staticmethod
    def pandas_dtypes() -> dict[str, str]:
        dtypes = EarthEngineProfile.pandas_dtypes().copy()
        dtypes.pop("station_id", None)
        dtypes.pop("lat", None)
        dtypes.pop("lon", None)
        dtypes["grid_cell_id"] = "string"
        return dtypes
