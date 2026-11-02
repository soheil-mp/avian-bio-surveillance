"""
Earth Engine Ingestion Module
=============================
Queries Google Earth Engine (GEE) to build comprehensive environmental
profiles for BirdWeather station locations (or custom coordinate lists).

The module contains two classes:

* :class:`EarthEngineProfiler` -- low-level GEE query engine that retrieves
  25+ raster datasets for a single ``(lat, lon, date)`` point.
* :class:`EarthEngineCollector` -- ingestion-pattern wrapper that discovers
  station locations (from DB, BirdWeather API, or local files), iterates
  over a date range, and saves results to ``data/raw/earth_engine/<date>/``.

Authentication
--------------
Set **one** of the following (checked in order):

1. ``GEE_SERVICE_ACCOUNT_KEY_FILE`` — path to a service-account JSON key.
2. ``GEE_PROJECT``                  — project ID (uses Application Default
   Credentials or interactive ``ee.Authenticate()``).

Examples::

    # Profile BirdWeather stations for Jan 2025
    poetry run python -m avian_biosurveillance.ingestion.earth_engine \\
        --start-date 2025-01-01 --end-date 2025-01-31

    # Single location, parallel mode
    poetry run python -m avian_biosurveillance.ingestion.earth_engine \\
        --lat 51.92 --lon 4.47 --date 2025-07-15 --parallel
"""

from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import tempfile
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from datetime import date as date_type
from pathlib import Path
from threading import RLock
from typing import Any, ClassVar

import ee
from geopy.distance import distance as geodesic_distance

from avian_biosurveillance.config.runtime import load_runtime_env
from avian_biosurveillance.db.models.earth_engine import (
    build_section_availability_from_profile,
)
from avian_biosurveillance.ingestion.earth_engine_cadence import (
    anchor_dates_in_range,
)
from avian_biosurveillance.utils.runtime_paths import preferred_temp_dir


load_runtime_env()

logger = logging.getLogger(__name__)


def _initialize_ee(project: str | None = None, high_volume: bool = False) -> None:
    """Initialise the Earth Engine API.

    Authentication is resolved in order:

    1. ``GEE_SERVICE_ACCOUNT_FILE`` env var — path to a service-account JSON
       key file. Preferred for production: file-mounted secrets are not
       visible via ``docker inspect`` or ``/proc/<pid>/environ``.
    2. ``GEE_SERVICE_ACCOUNT_CREDENTIALS`` env var — inline JSON string with
       the full service-account key (legacy; less secure).
    3. ``project`` arg / ``GEE_PROJECT`` env var — Application Default
       Credentials or interactive ``ee.Authenticate()``.
    """
    project = project or os.environ.get("GEE_PROJECT")
    credentials_file = os.environ.get("GEE_SERVICE_ACCOUNT_FILE")
    credentials_json = os.environ.get("GEE_SERVICE_ACCOUNT_CREDENTIALS")

    opt_url = "https://earthengine-highvolume.googleapis.com" if high_volume else None
    init_kwargs: dict[str, Any] = {}
    if project:
        init_kwargs["project"] = project
    if opt_url:
        init_kwargs["opt_url"] = opt_url

    credentials_path = Path(credentials_file) if credentials_file else None
    if credentials_path is not None and credentials_path.is_file():
        with credentials_path.open() as fh:
            key_data = json.load(fh)
        credentials = ee.ServiceAccountCredentials(
            key_data["client_email"],
            key_file=str(credentials_path),
        )
        init_kwargs["credentials"] = credentials
        if not project:
            init_kwargs["project"] = key_data.get("project_id")
        ee.Initialize(**init_kwargs)
    elif credentials_json:
        key_data = json.loads(credentials_json)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                delete=False,
                dir=preferred_temp_dir(),
            ) as tmp:
                json.dump(key_data, tmp)
                tmp_path = Path(tmp.name)
            credentials = ee.ServiceAccountCredentials(
                key_data["client_email"],
                key_file=str(tmp_path),
            )
            init_kwargs["credentials"] = credentials
            if not project:
                init_kwargs["project"] = key_data.get("project_id")
            ee.Initialize(**init_kwargs)
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
    else:
        ee.Initialize(**init_kwargs)


# ----------------------------------------------------------------------
#  Dataset-specific latency guards (days).  Queries inside these windows
#  will fall back to the most-recent pre-window composite instead of
#  returning empty data.
# ----------------------------------------------------------------------
LATENCY_DAYS: dict[str, int] = {
    "ECMWF/ERA5_LAND/DAILY_AGGR": 6,
    "MODIS/061/MOD13Q1": 22,  # 16-day composite, ~1 wk process lag
    "MODIS/061/MOD15A2H": 10,  # 8-day composite
    "MODIS/061/MOD16A2": 12,  # 8-day composite
    "MODIS/061/MOD11A1": 2,  # daily, ~1 d lag
    "MODIS/061/MOD10A1": 3,
    "MODIS/061/MCD64A1": 40,  # monthly, published ~1 mo later
    "MODIS/061/MOD17A2HGF": 14,
    "COPERNICUS/S5P/OFFL/L3_NO2": 5,
    "COPERNICUS/S5P/OFFL/L3_CO": 5,
    "COPERNICUS/S5P/OFFL/L3_SO2": 5,
    "COPERNICUS/S5P/OFFL/L3_O3": 5,
    "COPERNICUS/S5P/OFFL/L3_AER_AI": 5,
    "COPERNICUS/S5P/OFFL/L3_CH4": 7,
    "COPERNICUS/S5P/OFFL/L3_HCHO": 5,
    "COPERNICUS/S5P/OFFL/L3_CLOUD": 5,
    "COPERNICUS/S2_SR_HARMONIZED": 3,
    "COPERNICUS/S1_GRD": 3,
    "UCSB-CHG/CHIRPS/DAILY": 2,
    "FIRMS": 2,
    "NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG": 40,
    "NASA/VIIRS/002/VNP46A2": 5,
    "NOAA/CDR/OISST/V2_1": 3,
}


# ----------------------------------------------------------------------
#  Label maps (preserved from v1)
# ----------------------------------------------------------------------
DYNAMIC_WORLD_LABELS = {
    0: "water",
    1: "trees",
    2: "grass",
    3: "flooded_vegetation",
    4: "crops",
    5: "shrub_and_scrub",
    6: "built",
    7: "bare",
    8: "snow_and_ice",
}
DYNAMIC_WORLD_PROB_BANDS = (
    "water",
    "trees",
    "grass",
    "flooded_vegetation",
    "crops",
    "shrub_and_scrub",
    "built",
    "bare",
    "snow_and_ice",
)

ESA_WORLDCOVER_LABELS = {
    10: "tree_cover",
    20: "shrubland",
    30: "grassland",
    40: "cropland",
    50: "built_up",
    60: "bare_sparse_vegetation",
    70: "snow_and_ice",
    80: "permanent_water_body",
    90: "herbaceous_wetland",
    95: "mangroves",
    100: "moss_and_lichen",
}

MODIS_IGBP_LABELS = {
    1: "evergreen_needleleaf_forests",
    2: "evergreen_broadleaf_forests",
    3: "deciduous_needleleaf_forests",
    4: "deciduous_broadleaf_forests",
    5: "mixed_forests",
    6: "closed_shrublands",
    7: "open_shrublands",
    8: "woody_savannas",
    9: "savannas",
    10: "grasslands",
    11: "permanent_wetlands",
    12: "croplands",
    13: "urban_and_built_up_lands",
    14: "cropland_natural_vegetation_mosaics",
    15: "permanent_snow_and_ice",
    16: "barren",
    17: "water_bodies",
}


# ----------------------------------------------------------------------
#  Helpers
# ----------------------------------------------------------------------


def _parse_date(value: str | date_type | datetime) -> datetime:
    """Accept ``YYYY-MM-DD`` strings or ``date``/``datetime`` objects."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, date_type):
        return datetime(value.year, value.month, value.day)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse date {value!r}")


def _label_or_none(mapping: dict[int, str], value: Any) -> str | None:
    if value is None:
        return None
    try:
        iv = int(value)
    except (TypeError, ValueError):
        return str(value)
    return mapping.get(iv, str(iv))


def _round(value: Any, digits: int = 4) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _effective_date(collection_id: str, target: datetime) -> datetime:
    """Shift ``target`` back by the collection's latency window if needed."""
    lag = LATENCY_DAYS.get(collection_id, 0)
    if lag <= 0:
        return target
    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=lag)
    if target > cutoff:
        return cutoff
    return target


def _build_geometry(
    lat: float,
    lon: float,
    geometry: dict[str, Any] | None,
) -> tuple[Any, bool]:
    """Return (ee.Geometry, is_polygon).

    If ``geometry`` (GeoJSON Polygon) is provided, use it; otherwise fall
    back to ``ee.Geometry.Point``.
    """
    if geometry and geometry.get("type") == "Polygon":
        return ee.Geometry.Polygon(geometry["coordinates"]), True
    return ee.Geometry.Point(lon, lat), False


def _reduce_region(
    image: Any,
    geometry: Any,
    scale: int,
    *,
    reducer: Any | None = None,
    is_polygon: bool = False,
) -> dict[str, Any]:
    """Run ``image.reduceRegion`` and return the resulting dict.

    Uses ``ee.Reducer.mean()`` by default.  On polygons, caps to
    ``maxPixels`` = 1e9 to support large grid cells.  Raises
    ``ee.EEException`` on server errors (lets the caller decide).
    """
    red = reducer if reducer is not None else ee.Reducer.mean()
    kwargs: dict[str, Any] = {
        "reducer": red,
        "geometry": geometry,
        "scale": scale,
        "bestEffort": True,
    }
    if is_polygon:
        kwargs["maxPixels"] = int(1e9)
    result = image.reduceRegion(**kwargs).getInfo()
    return result or {}


def _latest_in_window(
    collection_id: str,
    geometry: Any,
    end: datetime,
    days: int,
) -> Any | None:
    """Return the most-recent image in ``[end - days, end]`` or ``None``.

    Used for latency-sensitive daily products where averaging is wrong
    (e.g. land-cover labels, surface water masks).
    """
    start = (end - timedelta(days=days)).strftime("%Y-%m-%d")
    end_s = (end + timedelta(days=1)).strftime("%Y-%m-%d")
    col = (
        ee.ImageCollection(collection_id)
        .filterDate(start, end_s)
        .filterBounds(geometry)
        .sort("system:time_start", False)
    )
    size = col.size().getInfo()
    if not size:
        return None
    return ee.Image(col.first())


def _composite_mean(
    collection_id: str,
    geometry: Any,
    center: datetime,
    half_window_days: int,
    bands: list[str] | None = None,
) -> tuple[Any, int] | tuple[None, int]:
    """Build a temporal-mean composite over a ± window."""
    effective = _effective_date(collection_id, center)
    start = (effective - timedelta(days=half_window_days)).strftime("%Y-%m-%d")
    end = (effective + timedelta(days=half_window_days + 1)).strftime("%Y-%m-%d")
    col = (
        ee.ImageCollection(collection_id).filterDate(start, end).filterBounds(geometry)
    )
    if bands:
        col = col.select(bands)
    size = col.size().getInfo()
    if not size:
        return None, 0
    return col.mean(), int(size)


def _safe_section(fn: Callable[[], dict[str, Any]], name: str) -> dict[str, Any]:
    """Run a section callable and distinguish "no data" from "failure"."""
    try:
        return fn()
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.warning("GEE section %s failed: %s", name, exc)
        return {"error": f"ee: {exc}"}
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("Unexpected error in section %s", name)
        return {"error": f"{type(exc).__name__}: {exc}"}


# ----------------------------------------------------------------------
#  Section implementations — each takes (lat, lon, date, geometry?)
# ----------------------------------------------------------------------


def get_elevation(
    lat: float,
    lon: float,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """SRTM 30 m elevation + slope + aspect."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    terrain = ee.Terrain.products(ee.Image("USGS/SRTMGL1_003"))
    stats = _reduce_region(
        terrain.select(["elevation", "slope", "aspect"]),
        geom,
        30,
        is_polygon=is_poly,
    )
    return {
        "elevation_m": _round(stats.get("elevation"), 1),
        "slope_degrees": _round(stats.get("slope"), 2),
        "aspect_degrees": _round(stats.get("aspect"), 1),
    }


def get_land_cover(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dynamic World (label + 9 probability bands), ESA WorldCover, MODIS IGBP, Copernicus LC 100 m."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)

    result: dict[str, Any] = {
        "dynamic_world": {"value": None, "label": None},
        "esa_worldcover": {"value": None, "label": None},
        "modis_igbp": {"value": None, "label": None},
    }
    dw_probs: dict[str, float | None] = {
        f"dw_{b}_prob": None for b in DYNAMIC_WORLD_PROB_BANDS
    }

    # Dynamic World — 30-day mean of probability bands + modal label
    dw_comp, _ = _composite_mean(
        "GOOGLE/DYNAMICWORLD/V1",
        geom,
        target,
        half_window_days=15,
        bands=list(DYNAMIC_WORLD_PROB_BANDS),
    )
    if dw_comp is not None:
        dw_mean = _reduce_region(dw_comp, geom, 10, is_polygon=is_poly)
        for b in DYNAMIC_WORLD_PROB_BANDS:
            dw_probs[f"dw_{b}_prob"] = _round(dw_mean.get(b), 4)
        # Label: argmax of mean probabilities
        probs = [
            (b, dw_mean.get(b))
            for b in DYNAMIC_WORLD_PROB_BANDS
            if dw_mean.get(b) is not None
        ]
        if probs:
            best = max(probs, key=lambda kv: kv[1])
            value = DYNAMIC_WORLD_PROB_BANDS.index(best[0])
            result["dynamic_world"] = {
                "value": value,
                "label": DYNAMIC_WORLD_LABELS.get(value),
            }

    # ESA WorldCover v200 (static, 2021)
    try:
        esa_img = ee.ImageCollection("ESA/WorldCover/v200").first()
        esa = _reduce_region(
            ee.Image(esa_img).select("Map"),
            geom,
            10,
            reducer=ee.Reducer.mode(),
            is_polygon=is_poly,
        )
        esa_val = _as_int(esa.get("Map"))
        result["esa_worldcover"] = {
            "value": esa_val,
            "label": _label_or_none(ESA_WORLDCOVER_LABELS, esa_val),
        }
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("ESA WorldCover lookup failed: %s", exc)

    # MODIS MCD12Q1 LC_Type1 (500 m, yearly)
    try:
        year = min(target.year, datetime.now(UTC).year - 1)
        mcd = (
            ee.ImageCollection("MODIS/061/MCD12Q1")
            .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
            .first()
        )
        modis = _reduce_region(
            ee.Image(mcd).select("LC_Type1"),
            geom,
            500,
            reducer=ee.Reducer.mode(),
            is_polygon=is_poly,
        )
        modis_val = _as_int(modis.get("LC_Type1"))
        result["modis_igbp"] = {
            "value": modis_val,
            "label": _label_or_none(MODIS_IGBP_LABELS, modis_val),
        }
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("MODIS IGBP lookup failed: %s", exc)

    # Copernicus Global Land Cover 100 m — cover fractions (use latest year ≤ 2019)
    cop_lc: dict[str, float | None] = {}
    try:
        cop_year = min(target.year, 2019)
        cop_img = ee.Image(f"COPERNICUS/Landcover/100m/Proba-V-C3/Global/{cop_year}")
        bands_map = {
            "cop_lc_tree_pct": "tree-coverfraction",
            "cop_lc_shrub_pct": "shrub-coverfraction",
            "cop_lc_grass_pct": "grass-coverfraction",
            "cop_lc_crops_pct": "crops-coverfraction",
            "cop_lc_urban_pct": "urban-coverfraction",
            "cop_lc_bare_pct": "bare-coverfraction",
            "cop_lc_water_permanent_pct": "water-permanent-coverfraction",
            "cop_lc_water_seasonal_pct": "water-seasonal-coverfraction",
            "cop_lc_moss_lichen_pct": "moss-coverfraction",
            "cop_lc_snow_ice_pct": "snow-coverfraction",
        }
        cop_stats = _reduce_region(
            cop_img.select(list(bands_map.values())),
            geom,
            100,
            is_polygon=is_poly,
        )
        for out_key, band in bands_map.items():
            cop_lc[out_key] = _round(cop_stats.get(band), 2)
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("Copernicus LC 100 m lookup failed: %s", exc)

    result.update(dw_probs)
    result.update(cop_lc)
    return result


def get_vegetation(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MODIS MOD13Q1 (NDVI/EVI/QA) + MOD15A2H (LAI/FPAR)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)

    result: dict[str, Any] = {
        "ndvi": None,
        "evi": None,
        "lai": None,
        "fpar": None,
        "ndvi_summary_qa": None,
    }

    # MOD13Q1 — 250 m, 16-day composite
    vi_comp, _ = _composite_mean(
        "MODIS/061/MOD13Q1",
        geom,
        target,
        half_window_days=16,
        bands=["NDVI", "EVI", "SummaryQA"],
    )
    if vi_comp is not None:
        vi = _reduce_region(vi_comp, geom, 250, is_polygon=is_poly)
        ndvi = vi.get("NDVI")
        evi = vi.get("EVI")
        qa = vi.get("SummaryQA")
        if ndvi is not None:
            result["ndvi"] = _round(float(ndvi) * 0.0001, 4)
        if evi is not None:
            result["evi"] = _round(float(evi) * 0.0001, 4)
        if qa is not None:
            result["ndvi_summary_qa"] = _as_int(round(float(qa)))

    # MOD15A2H — 500 m, 8-day composite
    la_comp, _ = _composite_mean(
        "MODIS/061/MOD15A2H",
        geom,
        target,
        half_window_days=8,
        bands=["Lai_500m", "Fpar_500m"],
    )
    if la_comp is not None:
        la = _reduce_region(la_comp, geom, 500, is_polygon=is_poly)
        lai = la.get("Lai_500m")
        fpar = la.get("Fpar_500m")
        if lai is not None:
            result["lai"] = _round(float(lai) * 0.1, 2)
        if fpar is not None:
            result["fpar"] = _round(float(fpar) * 0.01, 3)

    return result


def get_surface_temperature(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MODIS MOD11A1 LST day/night + QC (1 km, daily; ±2-day window)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)

    comp, _ = _composite_mean(
        "MODIS/061/MOD11A1",
        geom,
        target,
        half_window_days=2,
        bands=["LST_Day_1km", "LST_Night_1km", "QC_Day", "QC_Night"],
    )
    if comp is None:
        return {
            "lst_day_celsius": None,
            "lst_night_celsius": None,
            "lst_qc_day": None,
            "lst_qc_night": None,
        }
    stats = _reduce_region(comp, geom, 1000, is_polygon=is_poly)

    def _k_to_c(raw: Any) -> float | None:
        if raw is None:
            return None
        return _round(float(raw) * 0.02 - 273.15, 2)

    return {
        "lst_day_celsius": _k_to_c(stats.get("LST_Day_1km")),
        "lst_night_celsius": _k_to_c(stats.get("LST_Night_1km")),
        "lst_qc_day": _as_int(stats.get("QC_Day")),
        "lst_qc_night": _as_int(stats.get("QC_Night")),
    }


# Full ERA5-Land DAILY_AGGR band set that we persist.
ERA5_LAND_BANDS = [
    "temperature_2m",
    "dewpoint_2m",
    "total_precipitation_sum",
    "snowfall_sum",
    "snow_depth",
    "u_component_of_wind_10m",
    "v_component_of_wind_10m",
    "surface_pressure",
    "soil_temperature_level_1",
    "soil_temperature_level_2",
    "soil_temperature_level_3",
    "soil_temperature_level_4",
    "volumetric_soil_water_layer_1",
    "volumetric_soil_water_layer_2",
    "volumetric_soil_water_layer_3",
    "volumetric_soil_water_layer_4",
    "surface_solar_radiation_downwards_sum",
    "surface_thermal_radiation_downwards_sum",
    "surface_net_solar_radiation_sum",
    "surface_net_thermal_radiation_sum",
    "surface_latent_heat_flux_sum",
    "surface_sensible_heat_flux_sum",
    "total_evaporation_sum",
    "potential_evaporation_sum",
    "evaporation_from_bare_soil_sum",
    "evaporation_from_open_water_surfaces_excluding_oceans_sum",
    "evaporation_from_the_top_of_canopy_sum",
    "evaporation_from_vegetation_transpiration_sum",
    "runoff_sum",
    "surface_runoff_sum",
    "sub_surface_runoff_sum",
    "snow_cover",
    "snow_density",
    "snowmelt_sum",
    "snow_albedo",
    "leaf_area_index_high_vegetation",
    "leaf_area_index_low_vegetation",
    "skin_temperature",
]


def get_climate(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """ERA5-Land DAILY_AGGR — full ~35-band atmospheric / hydrological set."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    effective = _effective_date("ECMWF/ERA5_LAND/DAILY_AGGR", target)
    start = effective.strftime("%Y-%m-%d")
    end = (effective + timedelta(days=1)).strftime("%Y-%m-%d")

    # Some bands may not exist on all dates; select defensively.
    try:
        col = (
            ee.ImageCollection("ECMWF/ERA5_LAND/DAILY_AGGR")
            .filterDate(start, end)
            .filterBounds(geom)
        )
        if col.size().getInfo() == 0:
            return dict.fromkeys(_era5_output_keys())
        img = ee.Image(col.first())
        avail_bands = set(img.bandNames().getInfo())
        bands = [b for b in ERA5_LAND_BANDS if b in avail_bands]
        stats = _reduce_region(img.select(bands), geom, 11132, is_polygon=is_poly)
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.warning("ERA5-Land query failed: %s", exc)
        return dict.fromkeys(_era5_output_keys())

    # Unit conversions:
    #   temperature   K -> °C
    #   precipitation m -> mm
    #   snowfall      m (keep)
    t2m = stats.get("temperature_2m")
    d2m = stats.get("dewpoint_2m")
    skin = stats.get("skin_temperature")

    def _K2C(v: Any) -> float | None:
        if v is None:
            return None
        return _round(float(v) - 273.15, 2)

    def _M2MM(v: Any) -> float | None:
        if v is None:
            return None
        return _round(float(v) * 1000, 3)

    return {
        "temperature_2m_c": _K2C(t2m),
        "dewpoint_2m_c": _K2C(d2m),
        "skin_temperature_c": _K2C(skin),
        "total_precipitation_mm": _M2MM(stats.get("total_precipitation_sum")),
        "snowfall_m": _round(stats.get("snowfall_sum"), 4),
        "snow_depth_m": _round(stats.get("snow_depth"), 4),
        "era5_snow_cover": _round(stats.get("snow_cover"), 3),
        "era5_snow_density": _round(stats.get("snow_density"), 2),
        "era5_snowmelt_m": _round(stats.get("snowmelt_sum"), 5),
        "era5_snow_albedo": _round(stats.get("snow_albedo"), 3),
        "wind_u_10m_ms": _round(stats.get("u_component_of_wind_10m"), 2),
        "wind_v_10m_ms": _round(stats.get("v_component_of_wind_10m"), 2),
        "surface_pressure_pa": _round(stats.get("surface_pressure"), 1),
        "soil_temp_level1_c": _K2C(stats.get("soil_temperature_level_1")),
        "soil_temp_level2_c": _K2C(stats.get("soil_temperature_level_2")),
        "soil_temp_level3_c": _K2C(stats.get("soil_temperature_level_3")),
        "soil_temp_level4_c": _K2C(stats.get("soil_temperature_level_4")),
        "soil_moisture": _round(stats.get("volumetric_soil_water_layer_1"), 4),
        "soil_moisture_level2": _round(stats.get("volumetric_soil_water_layer_2"), 4),
        "soil_moisture_level3": _round(stats.get("volumetric_soil_water_layer_3"), 4),
        "soil_moisture_level4": _round(stats.get("volumetric_soil_water_layer_4"), 4),
        "surface_solar_rad_down_j_m2": _round(
            stats.get("surface_solar_radiation_downwards_sum"),
            1,
        ),
        "surface_thermal_rad_down_j_m2": _round(
            stats.get("surface_thermal_radiation_downwards_sum"),
            1,
        ),
        "surface_net_solar_rad_j_m2": _round(
            stats.get("surface_net_solar_radiation_sum"),
            1,
        ),
        "surface_net_thermal_rad_j_m2": _round(
            stats.get("surface_net_thermal_radiation_sum"),
            1,
        ),
        "surface_latent_heat_j_m2": _round(
            stats.get("surface_latent_heat_flux_sum"),
            1,
        ),
        "surface_sensible_heat_j_m2": _round(
            stats.get("surface_sensible_heat_flux_sum"),
            1,
        ),
        "total_evaporation_m": _round(stats.get("total_evaporation_sum"), 6),
        "potential_evaporation_m": _round(stats.get("potential_evaporation_sum"), 6),
        "evap_bare_soil_m": _round(stats.get("evaporation_from_bare_soil_sum"), 6),
        "evap_open_water_m": _round(
            stats.get("evaporation_from_open_water_surfaces_excluding_oceans_sum"),
            6,
        ),
        "evap_canopy_m": _round(
            stats.get("evaporation_from_the_top_of_canopy_sum"),
            6,
        ),
        "evap_vegetation_m": _round(
            stats.get("evaporation_from_vegetation_transpiration_sum"),
            6,
        ),
        "runoff_m": _round(stats.get("runoff_sum"), 6),
        "surface_runoff_m": _round(stats.get("surface_runoff_sum"), 6),
        "sub_surface_runoff_m": _round(stats.get("sub_surface_runoff_sum"), 6),
        "leaf_area_index_high": _round(
            stats.get("leaf_area_index_high_vegetation"),
            3,
        ),
        "leaf_area_index_low": _round(
            stats.get("leaf_area_index_low_vegetation"),
            3,
        ),
    }


def _era5_output_keys() -> list[str]:
    """Stable list of keys returned by :func:`get_climate` when data absent."""
    return [
        "temperature_2m_c",
        "dewpoint_2m_c",
        "skin_temperature_c",
        "total_precipitation_mm",
        "snowfall_m",
        "snow_depth_m",
        "era5_snow_cover",
        "era5_snow_density",
        "era5_snowmelt_m",
        "era5_snow_albedo",
        "wind_u_10m_ms",
        "wind_v_10m_ms",
        "surface_pressure_pa",
        "soil_temp_level1_c",
        "soil_temp_level2_c",
        "soil_temp_level3_c",
        "soil_temp_level4_c",
        "soil_moisture",
        "soil_moisture_level2",
        "soil_moisture_level3",
        "soil_moisture_level4",
        "surface_solar_rad_down_j_m2",
        "surface_thermal_rad_down_j_m2",
        "surface_net_solar_rad_j_m2",
        "surface_net_thermal_rad_j_m2",
        "surface_latent_heat_j_m2",
        "surface_sensible_heat_j_m2",
        "total_evaporation_m",
        "potential_evaporation_m",
        "evap_bare_soil_m",
        "evap_open_water_m",
        "evap_canopy_m",
        "evap_vegetation_m",
        "runoff_m",
        "surface_runoff_m",
        "sub_surface_runoff_m",
        "leaf_area_index_high",
        "leaf_area_index_low",
    ]


def _get_s5p_band(
    collection: str,
    band: str,
    geom: Any,
    target: datetime,
    half_window: int,
    is_poly: bool,
) -> float | None:
    comp, _ = _composite_mean(collection, geom, target, half_window, [band])
    if comp is None:
        return None
    stats = _reduce_region(comp, geom, 1113, is_polygon=is_poly)
    return stats.get(band)


def get_air_quality(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sentinel-5P full atmospheric panel: NO2, CO, SO2, O3, AER_AI, CH4, HCHO."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)

    specs = [
        (
            "no2_mol_m2",
            "COPERNICUS/S5P/OFFL/L3_NO2",
            "tropospheric_NO2_column_number_density",
            3,
            9,
        ),
        ("co_mol_m2", "COPERNICUS/S5P/OFFL/L3_CO", "CO_column_number_density", 3, 9),
        ("so2_mol_m2", "COPERNICUS/S5P/OFFL/L3_SO2", "SO2_column_number_density", 3, 9),
        ("ozone_mol_m2", "COPERNICUS/S5P/OFFL/L3_O3", "O3_column_number_density", 3, 9),
        (
            "aerosol_index",
            "COPERNICUS/S5P/OFFL/L3_AER_AI",
            "absorbing_aerosol_index",
            3,
            6,
        ),
        (
            "ch4_ppb",
            "COPERNICUS/S5P/OFFL/L3_CH4",
            "CH4_column_volume_mixing_ratio_dry_air",
            7,
            3,
        ),
        (
            "hcho_mol_m2",
            "COPERNICUS/S5P/OFFL/L3_HCHO",
            "tropospheric_HCHO_column_number_density",
            3,
            9,
        ),
    ]
    result: dict[str, Any] = {}
    for out_key, col, band, half_window, digits in specs:
        try:
            val = _get_s5p_band(col, band, geom, target, half_window, is_poly)
            result[out_key] = _round(val, digits) if val is not None else None
        except ee.EEException as exc:  # type: ignore[attr-defined]
            logger.debug("S5P %s unavailable: %s", out_key, exc)
            result[out_key] = None
    return result


def get_cloud_properties(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sentinel-5P cloud product."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    comp, _ = _composite_mean(
        "COPERNICUS/S5P/OFFL/L3_CLOUD",
        geom,
        target,
        3,
        [
            "cloud_fraction",
            "cloud_top_height",
            "cloud_base_height",
            "cloud_optical_depth",
        ],
    )
    if comp is None:
        return {
            "cloud_fraction": None,
            "cloud_cover_pct": None,
            "cloud_top_height_m": None,
            "cloud_base_height_m": None,
            "cloud_optical_depth": None,
        }
    stats = _reduce_region(comp, geom, 1113, is_polygon=is_poly)
    cf = stats.get("cloud_fraction")
    return {
        "cloud_fraction": _round(cf, 4) if cf is not None else None,
        "cloud_cover_pct": _round(float(cf) * 100, 2) if cf is not None else None,
        "cloud_top_height_m": _round(stats.get("cloud_top_height"), 1),
        "cloud_base_height_m": _round(stats.get("cloud_base_height"), 1),
        "cloud_optical_depth": _round(stats.get("cloud_optical_depth"), 3),
    }


def get_surface_water(
    lat: float,
    lon: float,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """JRC GSW1_4 — occurrence, change_abs, seasonality, recurrence, max_extent, transition."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    try:
        img = ee.Image("JRC/GSW1_4/GlobalSurfaceWater")
        cont_stats = _reduce_region(
            img.select(["occurrence", "change_abs", "seasonality", "recurrence"]),
            geom,
            30,
            is_polygon=is_poly,
        )
        cat_stats = _reduce_region(
            img.select(["max_extent", "transition"]),
            geom,
            30,
            reducer=ee.Reducer.mode(),
            is_polygon=is_poly,
        )
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("JRC GSW lookup failed: %s", exc)
        return {
            "surface_water_pct": None,
            "surface_water_change_abs": None,
            "surface_water_seasonality_months": None,
            "surface_water_recurrence_pct": None,
            "surface_water_max_extent": None,
            "surface_water_transition": None,
        }
    return {
        "surface_water_pct": _round(cont_stats.get("occurrence"), 2),
        "surface_water_change_abs": _round(cont_stats.get("change_abs"), 2),
        "surface_water_seasonality_months": _round(
            cont_stats.get("seasonality"),
            2,
        ),
        "surface_water_recurrence_pct": _round(cont_stats.get("recurrence"), 2),
        "surface_water_max_extent": _as_int(cat_stats.get("max_extent")),
        "surface_water_transition": _as_int(cat_stats.get("transition")),
    }


def get_forest(
    lat: float,
    lon: float,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Hansen 2025 forest change — treecover %, loss/gain, and loss year."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    try:
        img = ee.Image("UMD/hansen/global_forest_change_2025_v1_13")
        # treecover2000 → mean (continuous %)
        tc = _reduce_region(
            img.select("treecover2000"),
            geom,
            30,
            is_polygon=is_poly,
        )
        # loss / gain → max (any pixel) ⇒ boolean-any over cell
        any_stats = _reduce_region(
            img.select(["loss", "gain"]),
            geom,
            30,
            reducer=ee.Reducer.max(),
            is_polygon=is_poly,
        )
        # lossyear → mode (most common)
        ly_stats = _reduce_region(
            img.select("lossyear"),
            geom,
            30,
            reducer=ee.Reducer.mode(),
            is_polygon=is_poly,
        )
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("Hansen forest lookup failed: %s", exc)
        return {
            "forest_cover_pct": None,
            "forest_loss": None,
            "forest_gain": None,
            "forest_loss_year": None,
        }
    loss_val = any_stats.get("loss")
    gain_val = any_stats.get("gain")
    ly = ly_stats.get("lossyear")
    loss_year = None
    if ly is not None:
        ly_i = _as_int(round(float(ly)))
        if ly_i and ly_i > 0:
            loss_year = 2000 + ly_i
    return {
        "forest_cover_pct": _round(tc.get("treecover2000"), 2),
        "forest_loss": bool(loss_val) if loss_val is not None else None,
        "forest_gain": bool(gain_val) if gain_val is not None else None,
        "forest_loss_year": loss_year,
    }


def get_nighttime_lights(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """VIIRS monthly DNB — avg radiance + cloud-free coverage."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    # VIIRS monthly — take the most-recent monthly composite ≤ target
    try:
        col = (
            ee.ImageCollection("NOAA/VIIRS/DNB/MONTHLY_V1/VCMSLCFG")
            .filterDate("2012-04-01", (target + timedelta(days=1)).strftime("%Y-%m-%d"))
            .filterBounds(geom)
            .sort("system:time_start", False)
        )
        if col.size().getInfo() == 0:
            return {"nighttime_radiance_nw": None, "cloud_free_observations": None}
        img = ee.Image(col.first())
        stats = _reduce_region(
            img.select(["avg_rad", "cf_cvg"]),
            geom,
            500,
            is_polygon=is_poly,
        )
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("VIIRS lookup failed: %s", exc)
        return {"nighttime_radiance_nw": None, "cloud_free_observations": None}
    return {
        "nighttime_radiance_nw": _round(stats.get("avg_rad"), 3),
        "cloud_free_observations": _as_int(stats.get("cf_cvg")),
    }


def get_population(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """WorldPop (100 m, yearly, 2000-2020) + GPWv411 density."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    year_target = target.year
    year = min(year_target, 2020)  # WorldPop caps at 2020

    result: dict[str, Any] = {
        "worldpop_count": None,
        "gpw_density_per_km2": None,
        "population_source_year": year,
    }

    try:
        wp_col = (
            ee.ImageCollection("WorldPop/GP/100m/pop")
            .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
            .filterBounds(geom)
        )
        if wp_col.size().getInfo():
            img = ee.Image(wp_col.first())
            # On polygons we want TOTAL population (sum); on points we want
            # the sampled per-pixel estimate (mean).
            reducer = ee.Reducer.sum() if is_poly else ee.Reducer.mean()
            stats = _reduce_region(
                img.select("population"),
                geom,
                100,
                reducer=reducer,
                is_polygon=is_poly,
            )
            result["worldpop_count"] = _round(stats.get("population"), 2)
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("WorldPop lookup failed: %s", exc)

    try:
        gpw_year = min(year, 2020)
        gpw_col = ee.ImageCollection(
            "CIESIN/GPWv411/GPW_Population_Density"
        ).filterDate(f"{gpw_year}-01-01", f"{gpw_year + 5}-01-01")
        gpw_img = gpw_col.first()
        if gpw_img is not None:
            stats = _reduce_region(
                ee.Image(gpw_img).select("population_density"),
                geom,
                928,
                is_polygon=is_poly,
            )
            result["gpw_density_per_km2"] = _round(
                stats.get("population_density"),
                2,
            )
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("GPW lookup failed: %s", exc)

    return result


def get_human_modification(
    lat: float,
    lon: float,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """gHM 1 km global human modification index (2016)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    try:
        img = ee.ImageCollection("CSP/HM/GlobalHumanModification").first()
        stats = _reduce_region(
            ee.Image(img).select("gHM"),
            geom,
            1000,
            is_polygon=is_poly,
        )
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("gHM lookup failed: %s", exc)
        return {"human_modification_index": None}
    return {"human_modification_index": _round(stats.get("gHM"), 4)}


def get_fire(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MODIS burn date (MCD64A1) + FIRMS active fire (brightness, FRP, confidence)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)

    # Burn date — categorical DOY, use mode over 30-day window; latency 40 d
    burn = None
    try:
        b_comp, _ = _composite_mean(
            "MODIS/061/MCD64A1",
            geom,
            target,
            30,
            ["BurnDate"],
        )
        if b_comp is not None:
            bs = _reduce_region(
                b_comp,
                geom,
                500,
                reducer=ee.Reducer.mode(),
                is_polygon=is_poly,
            )
            burn = _as_int(bs.get("BurnDate"))
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("MCD64A1 lookup failed: %s", exc)

    fire_stats: dict[str, Any] = {
        "T21": None,
        "frp": None,
        "confidence": None,
    }
    try:
        f_comp, _ = _composite_mean(
            "FIRMS",
            geom,
            target,
            7,
            ["T21", "frp", "confidence"],
        )
        if f_comp is not None:
            fire_stats = _reduce_region(f_comp, geom, 1000, is_polygon=is_poly)
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("FIRMS lookup failed: %s", exc)

    return {
        "burned_area_date": burn,
        "fire_brightness_kelvin": _round(fire_stats.get("T21"), 1),
        "fire_frp_mw": _round(fire_stats.get("frp"), 2),
        "fire_confidence": _as_int(fire_stats.get("confidence")),
    }


# OpenLandMap soil datasets with depth-resolved bands.
# Keys are our column prefixes; values map depth name -> band name.
SOIL_DATASETS = {
    "clay_pct": (
        "OpenLandMap/SOL/SOL_CLAY-WFRACTION_USDA-3A1A1A_M/v02",
        {"0": "b0", "10": "b10", "30": "b30", "60": "b60"},
    ),
    "sand_pct": (
        "OpenLandMap/SOL/SOL_SAND-WFRACTION_USDA-3A1A1A_M/v02",
        {"0": "b0", "10": "b10", "30": "b30", "60": "b60"},
    ),
    "organic_carbon_g_kg": (
        "OpenLandMap/SOL/SOL_ORGANIC-CARBON_USDA-6A1C_M/v02",
        {"0": "b0", "10": "b10", "30": "b30", "60": "b60"},
    ),
    "soil_ph": (
        "OpenLandMap/SOL/SOL_PH-H2O_USDA-4C1A2A_M/v02",
        {"0": "b0", "10": "b10", "30": "b30", "60": "b60"},
    ),
    "bulk_density_kg_m3": (
        "OpenLandMap/SOL/SOL_BULKDENS-FINEEARTH_USDA-4A1H_M/v02",
        {"0": "b0", "10": "b10", "30": "b30", "60": "b60"},
    ),
}


def get_soil(
    lat: float,
    lon: float,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """OpenLandMap soil properties at 0/10/30/60 cm depths (250 m)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)

    # Flat "legacy" columns (0 cm) + full depth-resolved payload in residual JSON.
    result: dict[str, Any] = {}
    depth_result: dict[str, Any] = {}

    for prefix, (asset, depth_map) in SOIL_DATASETS.items():
        try:
            img = ee.Image(asset).select(list(depth_map.values()))
            stats = _reduce_region(img, geom, 250, is_polygon=is_poly)
        except ee.EEException as exc:  # type: ignore[attr-defined]
            logger.debug("OpenLandMap %s failed: %s", prefix, exc)
            stats = {}
        for depth, band in depth_map.items():
            raw = stats.get(band)
            val = None
            if raw is not None:
                val = float(raw) / 10 if prefix == "soil_ph" else float(raw)
                val = round(val, 2)
            # flat legacy column = 0 cm
            if depth == "0":
                result[prefix] = val
            depth_result[f"{prefix}_{depth}cm"] = val

    # Surface the depth map through the residual JSONB.
    result["_depths"] = depth_result
    return result


def get_evapotranspiration(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MODIS MOD16A2 ET / PET / LE / PLE / QC (500 m, 8-day)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    comp, _ = _composite_mean(
        "MODIS/061/MOD16A2",
        geom,
        target,
        8,
        ["ET", "PET", "LE", "PLE", "ET_QC"],
    )
    if comp is None:
        return {
            "et_mm_8day": None,
            "pet_mm_8day": None,
            "latent_heat_le_j_m2_day": None,
            "potential_le_j_m2_day": None,
            "et_qc": None,
        }
    stats = _reduce_region(comp, geom, 500, is_polygon=is_poly)
    et = stats.get("ET")
    pet = stats.get("PET")
    le = stats.get("LE")
    ple = stats.get("PLE")
    return {
        "et_mm_8day": _round(float(et) * 0.1, 2) if et is not None else None,
        "pet_mm_8day": _round(float(pet) * 0.1, 2) if pet is not None else None,
        "latent_heat_le_j_m2_day": _round(float(le) * 10000, 1)
        if le is not None
        else None,
        "potential_le_j_m2_day": _round(float(ple) * 10000, 1)
        if ple is not None
        else None,
        "et_qc": _as_int(stats.get("ET_QC")),
    }


def get_sea_surface_temperature(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """NOAA CDR OISST v2.1 — sea-surface temperature + anomaly."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    comp, _ = _composite_mean(
        "NOAA/CDR/OISST/V2_1",
        geom,
        target,
        3,
        ["sst", "anom"],
    )
    if comp is None:
        return {"sst_celsius": None, "sst_anom_celsius": None}
    stats = _reduce_region(comp, geom, 27830, is_polygon=is_poly)
    sst = stats.get("sst")
    anom = stats.get("anom")
    return {
        "sst_celsius": _round(float(sst) * 0.01, 2) if sst is not None else None,
        "sst_anom_celsius": _round(float(anom) * 0.01, 2) if anom is not None else None,
    }


# ----------------------------------------------------------------------
#  New datasets (Sentinel-2, Sentinel-1, CHIRPS, MODIS snow, MODIS GPP)
# ----------------------------------------------------------------------


def _s2_cloud_mask(image: Any) -> Any:
    """Mask clouds on a Sentinel-2 SR image using SCL band (6 = water, 4/5 = vegetation/bare)."""
    scl = image.select("SCL")
    # Valid classes: 4 veg, 5 bare, 6 water, 7 low-prob cloud
    valid = scl.eq(4).Or(scl.eq(5)).Or(scl.eq(6)).Or(scl.eq(7))
    return image.updateMask(valid)


def get_sentinel2(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sentinel-2 SR — NDVI, NDWI, MNDWI, NDMI, EVI, NBR @ 10 m (30-day composite)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    effective = _effective_date("COPERNICUS/S2_SR_HARMONIZED", target)
    start = (effective - timedelta(days=30)).strftime("%Y-%m-%d")
    end = (effective + timedelta(days=1)).strftime("%Y-%m-%d")

    empty = {
        "s2_ndvi": None,
        "s2_ndwi": None,
        "s2_mndwi": None,
        "s2_ndmi": None,
        "s2_evi": None,
        "s2_nbr": None,
        "s2_image_count": 0,
    }
    try:
        col = (
            ee.ImageCollection("COPERNICUS/S2_SR_HARMONIZED")
            .filterDate(start, end)
            .filterBounds(geom)
            .filter(ee.Filter.lt("CLOUDY_PIXEL_PERCENTAGE", 60))
            .map(_s2_cloud_mask)
        )
        n = col.size().getInfo()
        if not n:
            return empty
        comp = col.median()
        stats = _reduce_region(
            comp.select(["B2", "B3", "B4", "B8", "B11", "B12"]),
            geom,
            10,
            is_polygon=is_poly,
        )
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("Sentinel-2 query failed: %s", exc)
        return empty

    def _v(b: str) -> float | None:
        x = stats.get(b)
        return float(x) if x is not None else None

    B2 = _v("B2")
    B3 = _v("B3")
    B4 = _v("B4")
    B8 = _v("B8")
    B11 = _v("B11")
    B12 = _v("B12")

    def _norm(a: float | None, b: float | None) -> float | None:
        if a is None or b is None or (a + b) == 0:
            return None
        return round((a - b) / (a + b), 4)

    ndvi = _norm(B8, B4)
    ndwi = _norm(B3, B8)  # McFeeters
    mndwi = _norm(B3, B11)  # modified NDWI
    ndmi = _norm(B8, B11)  # moisture
    nbr = _norm(B8, B12)  # burn ratio
    evi = None
    if None not in (B8, B4, B2):
        denom = B8 + 6 * B4 - 7.5 * B2 + 1e4  # reflectance * 1e4 scale
        if denom != 0:
            evi = round(2.5 * (B8 - B4) / denom, 4)

    return {
        "s2_ndvi": ndvi,
        "s2_ndwi": ndwi,
        "s2_mndwi": mndwi,
        "s2_ndmi": ndmi,
        "s2_evi": evi,
        "s2_nbr": nbr,
        "s2_image_count": int(n),
    }


def get_sentinel1(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Sentinel-1 SAR GRD VV/VH (dB, 10 m, 30-day median composite)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    effective = _effective_date("COPERNICUS/S1_GRD", target)
    start = (effective - timedelta(days=30)).strftime("%Y-%m-%d")
    end = (effective + timedelta(days=1)).strftime("%Y-%m-%d")
    empty = {
        "s1_vv_db": None,
        "s1_vh_db": None,
        "s1_vv_vh_ratio_db": None,
        "s1_image_count": 0,
    }
    try:
        col = (
            ee.ImageCollection("COPERNICUS/S1_GRD")
            .filterDate(start, end)
            .filterBounds(geom)
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VV"))
            .filter(ee.Filter.listContains("transmitterReceiverPolarisation", "VH"))
            .filter(ee.Filter.eq("instrumentMode", "IW"))
        )
        n = col.size().getInfo()
        if not n:
            return empty
        comp = col.select(["VV", "VH"]).median()
        stats = _reduce_region(comp, geom, 10, is_polygon=is_poly)
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("Sentinel-1 query failed: %s", exc)
        return empty
    vv = stats.get("VV")
    vh = stats.get("VH")
    ratio = None
    if vv is not None and vh is not None:
        ratio = round(float(vv) - float(vh), 3)
    return {
        "s1_vv_db": _round(vv, 3),
        "s1_vh_db": _round(vh, 3),
        "s1_vv_vh_ratio_db": ratio,
        "s1_image_count": int(n),
    }


def get_chirps(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """CHIRPS 5.5 km daily precipitation (mm/day)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    effective = _effective_date("UCSB-CHG/CHIRPS/DAILY", target)
    day = effective.strftime("%Y-%m-%d")
    next_day = (effective + timedelta(days=1)).strftime("%Y-%m-%d")
    try:
        col = (
            ee.ImageCollection("UCSB-CHG/CHIRPS/DAILY")
            .filterDate(day, next_day)
            .filterBounds(geom)
        )
        if col.size().getInfo() == 0:
            return {"chirps_precip_mm": None}
        stats = _reduce_region(
            ee.Image(col.first()).select("precipitation"),
            geom,
            5566,
            is_polygon=is_poly,
        )
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("CHIRPS query failed: %s", exc)
        return {"chirps_precip_mm": None}
    return {"chirps_precip_mm": _round(stats.get("precipitation"), 3)}


def get_modis_snow(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MODIS MOD10A1 snow-cover fraction + NDSI (500 m, daily, ±4-day window)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    comp, _ = _composite_mean(
        "MODIS/061/MOD10A1",
        geom,
        target,
        4,
        ["NDSI_Snow_Cover", "NDSI"],
    )
    if comp is None:
        return {"modis_snow_cover_pct": None, "modis_ndsi": None}
    stats = _reduce_region(comp, geom, 500, is_polygon=is_poly)
    return {
        "modis_snow_cover_pct": _round(stats.get("NDSI_Snow_Cover"), 2),
        "modis_ndsi": _round(stats.get("NDSI"), 4),
    }


def get_modis_productivity(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MODIS MOD17A2HGF GPP + PsnNet (500 m, 8-day)."""
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    comp, _ = _composite_mean(
        "MODIS/061/MOD17A2HGF",
        geom,
        target,
        8,
        ["Gpp", "PsnNet"],
    )
    if comp is None:
        return {"modis_gpp_kg_c_m2": None, "modis_psn_net_kg_c_m2": None}
    stats = _reduce_region(comp, geom, 500, is_polygon=is_poly)
    gpp = stats.get("Gpp")
    psn = stats.get("PsnNet")
    # Scale factor 0.0001 per MODIS spec
    return {
        "modis_gpp_kg_c_m2": _round(float(gpp) * 0.0001, 6)
        if gpp is not None
        else None,
        "modis_psn_net_kg_c_m2": _round(float(psn) * 0.0001, 6)
        if psn is not None
        else None,
    }


def get_phenology(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """MODIS MCD12Q2 land-surface phenology (annual, 500 m, cycle 1).

    Emits day-of-year for key phenophase transition dates (greenup,
    mid-greenup, peak, maturity, senescence, dormancy) plus EVI2
    amplitude/area and QA. Uses the most-recent year ≤ ``date_str.year``
    with available data (MCD12Q2 publishes ~1 year in arrears).
    """
    empty = {
        "phenology_greenup_doy": None,
        "phenology_midgreenup_doy": None,
        "phenology_peak_doy": None,
        "phenology_maturity_doy": None,
        "phenology_senescence_doy": None,
        "phenology_dormancy_doy": None,
        "phenology_evi_amplitude": None,
        "phenology_evi_area": None,
        "phenology_qa": None,
    }
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    # MCD12Q2 is annual and lags by ~1 year.
    year = min(target.year, datetime.now(UTC).year - 1)
    try:
        col = (
            ee.ImageCollection("MODIS/061/MCD12Q2")
            .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
            .filterBounds(geom)
        )
        if col.size().getInfo() == 0:
            # Fallback one more year back in case of publication lag.
            year -= 1
            col = (
                ee.ImageCollection("MODIS/061/MCD12Q2")
                .filterDate(f"{year}-01-01", f"{year + 1}-01-01")
                .filterBounds(geom)
            )
            if col.size().getInfo() == 0:
                return empty
        img = ee.Image(col.first())
        bands = [
            "Greenup_1",
            "MidGreenup_1",
            "Peak_1",
            "Maturity_1",
            "Senescence_1",
            "Dormancy_1",
            "EVI_Amplitude_1",
            "EVI_Area_1",
            "QA_Overall_1",
        ]
        stats = _reduce_region(
            img.select(bands),
            geom,
            500,
            is_polygon=is_poly,
        )
    except ee.EEException as exc:  # type: ignore[attr-defined]
        logger.debug("MCD12Q2 phenology lookup failed: %s", exc)
        return empty

    # Transition-date bands are "days since 1970-01-01". Convert to DOY of
    # the tile's year for a more interpretable, model-friendly signal.
    epoch = date_type(1970, 1, 1)

    def _to_doy(val: Any) -> int | None:
        days = _as_int(val)
        if days is None:
            return None
        try:
            return (epoch + timedelta(days=days)).timetuple().tm_yday
        except (OverflowError, ValueError):
            return None

    # EVI amplitude/area are stored as EVI2 x 10000 / x 100 (per spec).
    amp = stats.get("EVI_Amplitude_1")
    area = stats.get("EVI_Area_1")
    return {
        "phenology_greenup_doy": _to_doy(stats.get("Greenup_1")),
        "phenology_midgreenup_doy": _to_doy(stats.get("MidGreenup_1")),
        "phenology_peak_doy": _to_doy(stats.get("Peak_1")),
        "phenology_maturity_doy": _to_doy(stats.get("Maturity_1")),
        "phenology_senescence_doy": _to_doy(stats.get("Senescence_1")),
        "phenology_dormancy_doy": _to_doy(stats.get("Dormancy_1")),
        "phenology_evi_amplitude": (
            _round(float(amp) * 0.0001, 4) if amp is not None else None
        ),
        "phenology_evi_area": (
            _round(float(area) * 0.01, 2) if area is not None else None
        ),
        "phenology_qa": _as_int(stats.get("QA_Overall_1")),
    }


def get_black_marble(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """NASA Black Marble VNP46A2 — BRDF-corrected daily nighttime lights.

    Higher-quality urban-intensity proxy than the monthly VCMSLCFG
    (already exposed via ``get_nighttime_lights``). Uses a ±15-day mean
    of ``Gap_Filled_DNB_BRDF_Corrected_NTL`` around ``date_str`` and
    returns the modal ``Mandatory_Quality_Flag`` for that window.
    """
    geom, is_poly = _build_geometry(lat, lon, geometry)
    target = _parse_date(date_str)
    comp, _ = _composite_mean(
        "NASA/VIIRS/002/VNP46A2",
        geom,
        target,
        15,
        ["Gap_Filled_DNB_BRDF_Corrected_NTL", "Mandatory_Quality_Flag"],
    )
    if comp is None:
        return {
            "black_marble_ntl_nw": None,
            "black_marble_qa": None,
        }
    ntl_stats = _reduce_region(
        comp.select(["Gap_Filled_DNB_BRDF_Corrected_NTL"]),
        geom,
        500,
        is_polygon=is_poly,
    )
    # Scale factor 0.1 per VNP46A2 spec → nW·cm⁻²·sr⁻¹.
    raw = ntl_stats.get("Gap_Filled_DNB_BRDF_Corrected_NTL")
    ntl = _round(float(raw) * 0.1, 3) if raw is not None else None

    # QA: modal flag over the window (categorical, averaging is meaningless).
    qa_img, _ = _composite_mean(
        "NASA/VIIRS/002/VNP46A2",
        geom,
        target,
        15,
        ["Mandatory_Quality_Flag"],
    )
    qa_val: int | None = None
    if qa_img is not None:
        try:
            qa_stats = (
                qa_img.select("Mandatory_Quality_Flag")
                .reduceRegion(
                    reducer=ee.Reducer.mode(),
                    geometry=geom,
                    scale=500,
                    bestEffort=True,
                    maxPixels=int(1e9) if is_poly else 1_000_000,
                )
                .getInfo()
                or {}
            )
            qa_val = _as_int(qa_stats.get("Mandatory_Quality_Flag"))
        except ee.EEException as exc:  # type: ignore[attr-defined]
            logger.debug("VNP46A2 QA mode failed: %s", exc)
    return {
        "black_marble_ntl_nw": ntl,
        "black_marble_qa": qa_val,
    }


# ----------------------------------------------------------------------
#  Section registry + orchestrator
# ----------------------------------------------------------------------

# Ordered list of (section name, callable, requires_date).
SECTIONS: list[tuple[str, Callable[..., dict[str, Any]], bool]] = [
    ("elevation", get_elevation, False),
    ("land_cover", get_land_cover, True),
    ("vegetation", get_vegetation, True),
    ("surface_temperature", get_surface_temperature, True),
    ("climate", get_climate, True),
    ("air_quality", get_air_quality, True),
    ("cloud_properties", get_cloud_properties, True),
    ("surface_water", get_surface_water, False),
    ("forest", get_forest, False),
    ("nighttime_lights", get_nighttime_lights, True),
    ("population", get_population, True),
    ("human_modification", get_human_modification, False),
    ("fire", get_fire, True),
    ("soil", get_soil, False),
    ("evapotranspiration", get_evapotranspiration, True),
    ("sea_surface_temperature", get_sea_surface_temperature, True),
    ("sentinel2", get_sentinel2, True),
    ("sentinel1", get_sentinel1, True),
    ("chirps", get_chirps, True),
    ("modis_snow", get_modis_snow, True),
    ("modis_productivity", get_modis_productivity, True),
    ("phenology", get_phenology, True),
    ("black_marble", get_black_marble, True),
]


def run_section(
    name: str,
    lat: float,
    lon: float,
    date_str: str | None,
    *,
    geometry: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Dispatch a single section by name."""
    for sname, fn, needs_date in SECTIONS:
        if sname != name:
            continue
        if needs_date:
            return _safe_section(
                lambda f=fn: f(lat, lon, date_str or "", geometry=geometry),
                name,
            )
        return _safe_section(lambda f=fn: f(lat, lon, geometry=geometry), name)
    raise KeyError(f"Unknown section: {name}")


def run_all(
    lat: float,
    lon: float,
    date_str: str,
    *,
    geometry: dict[str, Any] | None = None,
    section_names: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    """Run every registered section (or ``section_names`` subset)."""
    wanted: set[str] | None = set(section_names) if section_names else None
    out: dict[str, dict[str, Any]] = {}
    for sname, fn, needs_date in SECTIONS:
        if wanted and sname not in wanted:
            continue
        if needs_date:
            out[sname] = _safe_section(
                lambda f=fn: f(lat, lon, date_str, geometry=geometry),
                sname,
            )
        else:
            out[sname] = _safe_section(
                lambda f=fn: f(lat, lon, geometry=geometry),
                sname,
            )
    return out


# ----------------------------------------------------------------------
#  Backward-compatible public class
# ----------------------------------------------------------------------


class EarthEngineProfiler:
    """Thin wrapper that initialises the GEE SDK and exposes the section
    registry from this module.

    The heavy lifting (one ``reduceRegion`` per section, polygon-aware
    sampling, latency-guarded composites) lives in the module-level
    ``get_<section>`` functions above and the :data:`SECTIONS` registry.
    """

    def __init__(self, project: str | None = None, high_volume: bool = False):
        _initialize_ee(project=project, high_volume=high_volume)

    @staticmethod
    def get_full_profile(
        lat: float,
        lon: float,
        date_str: str,
        *,
        parallel: bool = True,  # kept for CLI compatibility; run_all is sequential
        sections: list[str] | None = None,
        geometry: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Delegate to :func:`run_all`."""
        del parallel  # sequential — callers that need parallelism use the collectors
        return run_all(lat, lon, date_str, geometry=geometry, section_names=sections)

    @staticmethod
    def print_report(profile: dict, indent: int = 0) -> None:
        """Recursively pretty-print a profile dictionary."""
        pad = "  " * indent
        for key, value in profile.items():
            if isinstance(value, dict):
                sys.stdout.write(f"{pad}{key}:\n")
                EarthEngineProfiler.print_report(value, indent + 1)
            else:
                sys.stdout.write(f"{pad}{key}: {value}\n")


# ====================================================================== #
#  Ingestion Collector                                                    #
# ====================================================================== #

_BW_ROOT = Path(__file__).resolve().parents[3] / "data" / "raw" / "birdweather"
_EE_ROOT = Path(__file__).resolve().parents[3] / "data" / "raw" / "earth_engine"
_EE_GRID_ROOT = (
    Path(__file__).resolve().parents[3] / "data" / "raw" / "earth_engine_grid"
)
_PDOK_COUNTRY_WFS_URL = (
    "https://service.pdok.nl/kadaster/bestuurlijkegebieden/wfs/v1_0?"
    "service=WFS&version=2.0.0&request=GetFeature&"
    "typeName=bestuurlijkegebieden:Landgebied&"
    "outputFormat=json&srsName=EPSG:4326"
)
DEFAULT_EE_GRID_SIZE_KM = 5.0


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    """Read a positive integer environment override."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return max(minimum, int(raw))
    except ValueError:
        logger.warning(
            "Ignoring invalid %s=%r; using default %d.",
            name,
            raw,
            default,
        )
        return default


DEFAULT_EE_SECTION_WORKERS = _env_int("EARTH_ENGINE_SECTION_WORKERS", 4)
DEFAULT_EE_GRID_CELL_WORKERS = _env_int("EARTH_ENGINE_GRID_CELL_WORKERS", 2)
DEFAULT_EE_GRID_DB_BATCH_SIZE = _env_int("EARTH_ENGINE_GRID_DB_BATCH_SIZE", 200)
DEFAULT_EE_HTTP_POOL_SIZE = _env_int("EARTH_ENGINE_HTTP_POOL_SIZE", 10)


@dataclass
class _LocationSpec:
    """A point to profile, with provenance metadata."""

    station_id: str
    source: str  # e.g. "birdweather"
    lat: float
    lon: float


@dataclass(frozen=True)
class _GridCellSpec:
    """A canonical grid cell represented by centroid, bbox, and polygon."""

    grid_cell_id: str
    grid_system: str
    source: str
    lat: float
    lon: float
    bbox_min_lat: float
    bbox_min_lon: float
    bbox_max_lat: float
    bbox_max_lon: float
    grid_size_km: float
    cell_geojson: dict[str, Any]
    grid_row: int | None = None
    grid_col: int | None = None


@dataclass(frozen=True)
class _BoundaryPolygon:
    """One polygon component from a country boundary GeoJSON payload."""

    outer_ring: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...]
    bbox_min_lat: float
    bbox_min_lon: float
    bbox_max_lat: float
    bbox_max_lon: float


@dataclass
class CollectStats:
    """Summary returned by :meth:`EarthEngineCollector.collect_for_date`."""

    date: str
    locations_profiled: int = 0
    locations_skipped: int = 0
    locations_failed: int = 0
    elapsed_s: float = 0.0

    def __str__(self) -> str:
        skipped = (
            f", {self.locations_skipped} skipped"
            if self.locations_skipped
            else ""
        )
        return (
            f"{self.date}: {self.locations_profiled} profiled, "
            f"{self.locations_failed} failed{skipped} ({self.elapsed_s:.1f}s)"
        )


class EarthEngineCollector:
    """Ingestion-pattern wrapper around :class:`EarthEngineProfiler`.

    Discovers BirdWeather station locations, profiles each one through
    GEE, and saves results to ``data/raw/earth_engine/<date>/``.

    Output structure::

        data/raw/earth_engine/
          YYYY-MM-DD/
            profiles.json     # List of per-station environmental profiles
            metadata.json     # Collection metadata
    """

    # Which profiler sections to include (all by default).  v2 adds
    # Sentinel-2 indices, Sentinel-1 SAR, CHIRPS precipitation, MODIS
    # snow, and MODIS GPP on top of the original 16 sections.
    _SECTIONS: ClassVar[list[str]] = [
        "elevation",
        "land_cover",
        "vegetation",
        "surface_temperature",
        "climate",
        "air_quality",
        "cloud_properties",
        "surface_water",
        "forest",
        "nighttime_lights",
        "population",
        "human_modification",
        "fire",
        "soil",
        "evapotranspiration",
        "sea_surface_temperature",
        "sentinel2",
        "sentinel1",
        "chirps",
        "modis_snow",
        "modis_productivity",
    ]

    def __init__(
        self,
        output_dir: Path | None = None,
        project: str | None = None,
        high_volume: bool = False,
        parallel: bool = True,
        max_workers: int = DEFAULT_EE_SECTION_WORKERS,
        sections: list[str] | None = None,
    ) -> None:
        """Initialise the collector.

        Args:
            output_dir:  Root directory for saved data.  Defaults to
                         ``data/raw/earth_engine``.
            project:     GEE project ID (falls back to env vars).
            high_volume: Use the GEE high-volume endpoint.
            parallel:    Profile each station's sections in parallel.
            max_workers: Thread pool size for parallel profiling.
            sections:    Subset of profiler sections to query.
        """
        self.output_dir = output_dir or _EE_ROOT
        self.parallel = parallel
        self.max_workers = max_workers
        self.sections = sections or self._SECTIONS
        self._profiler = EarthEngineProfiler(
            project=project,
            high_volume=high_volume,
        )
        self._static_section_cache: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._static_section_cache_lock = RLock()

    # ------------------------------------------------------------------ #
    #  Location discovery (API → DB → local files)                        #
    # ------------------------------------------------------------------ #

    # Lightweight GraphQL query — only fetches id, name, and coords.
    _STATIONS_QUERY = """
    query Stations(
        $ne: InputLocation, $sw: InputLocation, $cursor: String
    ) {
      stations(ne: $ne, sw: $sw, first: 50, after: $cursor) {
        totalCount
        pageInfo { hasNextPage endCursor }
        nodes { id name coords { lat lon } }
      }
    }
    """

    # Netherlands bounding box.
    _NL_NE: ClassVar[dict[str, float]] = {"lat": 53.6, "lon": 7.3}
    _NL_SW: ClassVar[dict[str, float]] = {"lat": 50.7, "lon": 3.3}

    _BW_API = "https://app.birdweather.com/graphql"
    _BW_HEADERS: ClassVar[dict[str, str]] = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    @staticmethod
    def discover_locations() -> list[_LocationSpec]:
        """Auto-discover BirdWeather station locations.

        Tries three sources in order (first success wins):

        1. **BirdWeather API** — public GraphQL endpoint (no token
           required, works anywhere with network access).
        2. **Database** — ``birdweather_stations_tbl`` table (Docker with
           populated DB).
        3. **Local files** — ``stations.jsonl`` under
           ``data/raw/birdweather/`` (dev / local runs only).

        Returns:
            Deduplicated list of :class:`_LocationSpec`.
        """
        # --- 1. Try BirdWeather API (public, no auth) ---
        locs = EarthEngineCollector._discover_from_api()
        if locs:
            return locs

        # --- 2. Try database ---
        locs = EarthEngineCollector._discover_from_db()
        if locs:
            return locs

        # --- 3. Fall back to local stations.jsonl ---
        return EarthEngineCollector._discover_from_local_files()

    @staticmethod
    def _discover_from_api() -> list[_LocationSpec]:
        """Query BirdWeather public GraphQL API for NL stations.

        Uses a lightweight query (id + coords only) and handles
        pagination.  No authentication token is required.
        """
        import requests as _requests

        try:
            by_id: dict[str, _LocationSpec] = {}
            cursor: str | None = None

            while True:
                payload = {
                    "query": EarthEngineCollector._STATIONS_QUERY,
                    "variables": {
                        "ne": EarthEngineCollector._NL_NE,
                        "sw": EarthEngineCollector._NL_SW,
                        "cursor": cursor,
                    },
                }
                resp = _requests.post(
                    EarthEngineCollector._BW_API,
                    json=payload,
                    headers=EarthEngineCollector._BW_HEADERS,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                conn = data.get("data", {}).get("stations", {})
                nodes = conn.get("nodes", [])
                page_info = conn.get("pageInfo", {})

                for node in nodes:
                    sid = str(node.get("id", ""))
                    coords = node.get("coords", {})
                    lat, lon = coords.get("lat"), coords.get("lon")
                    if sid and lat is not None and lon is not None:
                        by_id[sid] = _LocationSpec(
                            station_id=sid,
                            source="birdweather_api",
                            lat=float(lat),
                            lon=float(lon),
                        )

                new_cursor = page_info.get("endCursor")
                if (
                    not new_cursor
                    or new_cursor == cursor
                    or not page_info.get("hasNextPage")
                ):
                    break
                cursor = new_cursor
                time.sleep(0.2)

            if not by_id:
                logger.debug("BirdWeather API returned no stations")
                return []

            logger.info(
                "Discovered %d stations from BirdWeather API",
                len(by_id),
            )
            return list(by_id.values())
        except Exception:
            logger.debug(
                "BirdWeather API unavailable for station discovery",
                exc_info=True,
            )
            return []

    @staticmethod
    def _discover_from_db() -> list[_LocationSpec]:
        """Query ``birdweather_stations_tbl`` for distinct station locations."""
        try:
            from avian_biosurveillance.db.database import get_pool

            pool = get_pool()
            with pool.cursor() as cur:
                cur.execute(
                    "SELECT station_id, lat, lon "
                    "FROM birdweather_stations_tbl "
                    "WHERE lat IS NOT NULL AND lon IS NOT NULL",
                )
                rows = cur.fetchall()

            if not rows:
                logger.debug("birdweather_stations_tbl is empty")
                return []

            locs = [
                _LocationSpec(
                    station_id=str(r[0]),
                    source="database",
                    lat=float(r[1]),
                    lon=float(r[2]),
                )
                for r in rows
            ]
            logger.info(
                "Discovered %d stations from database",
                len(locs),
            )
            return locs
        except Exception:
            logger.debug(
                "Database not available for station discovery",
                exc_info=True,
            )
            return []

    @staticmethod
    def _discover_from_local_files(
        bw_root: Path | None = None,
    ) -> list[_LocationSpec]:
        """Read unique stations from local ``stations.jsonl`` files."""
        root = bw_root or _BW_ROOT
        if not root.exists():
            logger.debug("BirdWeather data dir not found: %s", root)
            return []

        by_id: dict[str, _LocationSpec] = {}
        for sfile in sorted(root.glob("*/stations.jsonl")):
            with sfile.open(encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    rec = json.loads(line)
                    sid = str(rec.get("id", ""))
                    coords = rec.get("coords", {})
                    lat = coords.get("lat")
                    lon = coords.get("lon")
                    if sid and lat is not None and lon is not None:
                        by_id[sid] = _LocationSpec(
                            station_id=sid,
                            source="birdweather",
                            lat=float(lat),
                            lon=float(lon),
                        )

        if by_id:
            logger.info(
                "Discovered %d stations from local files",
                len(by_id),
            )
        else:
            logger.debug("No stations.jsonl files found")
        return list(by_id.values())

    @staticmethod
    def load_locations_file(path: Path) -> list[_LocationSpec]:
        """Load locations from a user-supplied JSON/JSONL file.

        Expected format per record::

            {"id": "...", "lat": 51.92, "lon": 4.47}
        """
        locations: list[_LocationSpec] = []
        with path.open(encoding="utf-8") as fh:
            # Try JSON array first, fall back to JSONL
            text = fh.read().strip()
        if text.startswith("["):
            for rec in json.loads(text):
                locations.append(
                    _LocationSpec(
                        station_id=str(rec["id"]),
                        source="custom",
                        lat=float(rec["lat"]),
                        lon=float(rec["lon"]),
                    )
                )
        else:
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                rec = json.loads(line)
                locations.append(
                    _LocationSpec(
                        station_id=str(rec["id"]),
                        source="custom",
                        lat=float(rec["lat"]),
                        lon=float(rec["lon"]),
                    )
                )
        return locations

    # ------------------------------------------------------------------ #
    #  Single-station profiling                                           #
    # ------------------------------------------------------------------ #

    def _profile_station(
        self,
        loc: _LocationSpec,
        date_str: str,
        *,
        geometry: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Query GEE for a single station/grid-cell on a single date.

        Dispatches to the module-level :data:`SECTIONS` registry defined
        above. When ``geometry`` is a GeoJSON Polygon, sampling is done
        over the full cell instead of at the centroid (fixes the 98 %
        NULL surface-water issue on 5 km grid cells).
        """
        # Touch the profiler to make sure ee.Initialize() has run.
        _ = self._profiler

        started = time.perf_counter()
        profile: dict[str, Any] = {
            "station_id": loc.station_id,
            "source": loc.source,
            "lat": loc.lat,
            "lon": loc.lon,
            "date": date_str,
        }

        section_requires_date = {n: needs_date for n, _f, needs_date in SECTIONS}
        sections_to_run = [
            s for s in self.sections if s in section_requires_date
        ]

        def _run_one(name: str) -> tuple[str, dict[str, Any]]:
            if not section_requires_date[name]:
                return name, self._run_static_section(
                    loc,
                    name,
                    geometry=geometry,
                )
            return name, run_section(
                name,
                loc.lat,
                loc.lon,
                date_str,
                geometry=geometry,
            )

        if self.parallel:
            with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
                futures = {pool.submit(_run_one, n): n for n in sections_to_run}
                for fut in as_completed(futures):
                    name, payload = fut.result()
                    profile[name] = payload
        else:
            for name in sections_to_run:
                _, payload = _run_one(name)
                profile[name] = payload

        profile["_meta"] = {
            "mode": "parallel" if self.parallel else "sequential",
            "total_seconds": round(time.perf_counter() - started, 2),
            "profiler_version": "v2",
            "geometry_type": (
                "polygon" if geometry and geometry.get("type") == "Polygon" else "point"
            ),
        }
        profile["section_availability"] = build_section_availability_from_profile(
            profile,
        )
        return profile

    @staticmethod
    def _static_section_cache_key(
        loc: _LocationSpec,
        section_name: str,
        geometry: dict[str, Any] | None,
    ) -> tuple[str, str, str]:
        """Return a stable cache key for sections that do not depend on date."""
        geometry_key = (
            json.dumps(geometry, sort_keys=True, separators=(",", ":"))
            if geometry is not None
            else "point"
        )
        return (loc.station_id, section_name, geometry_key)

    def _run_static_section(
        self,
        loc: _LocationSpec,
        section_name: str,
        *,
        geometry: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """Run or reuse a date-invariant section for one location."""
        key = self._static_section_cache_key(loc, section_name, geometry)
        with self._static_section_cache_lock:
            cached = self._static_section_cache.get(key)
        if cached is not None:
            return copy.deepcopy(cached)

        payload = run_section(
            section_name,
            loc.lat,
            loc.lon,
            None,
            geometry=geometry,
        )
        if not payload.get("error"):
            with self._static_section_cache_lock:
                self._static_section_cache[key] = copy.deepcopy(payload)
        return payload

    # ------------------------------------------------------------------ #
    #  Date-level collection                                              #
    # ------------------------------------------------------------------ #

    def collect_for_date(
        self,
        target_date: date_type,
        locations: list[_LocationSpec],
        *,
        target: str = "local",
        force: bool = False,
    ) -> CollectStats:
        """Profile all *locations* for a single *target_date*.

        Args:
            target_date: Date to query.
            locations:   Coordinates to profile.
            target:      Storage target — ``"local"`` (default) or
                         ``"database"``.
            force:       Re-run even if output already exists.

        Returns:
            :class:`CollectStats` summary.
        """
        persist_to_db = target == "database"
        if persist_to_db and set(self.sections) != set(self._SECTIONS):
            msg = (
                "Database target requires a full Earth Engine profile. "
                "Partial section runs are only supported for local output "
                "because omitted sections would overwrite existing DB values."
            )
            raise ValueError(msg)
        save_local = target == "local"
        date_str = target_date.isoformat()
        day_dir = Path(self.output_dir) / date_str
        meta_file = day_dir / "metadata.json"

        # Smart skip: existing complete run
        if save_local and not force and meta_file.exists():
            try:
                prev = json.loads(meta_file.read_text(encoding="utf-8"))
                if prev.get("run", {}).get("completed"):
                    logger.info(
                        "Skipping %s — already complete (use --force to re-run)",
                        date_str,
                    )
                    return CollectStats(date=date_str)
            except (json.JSONDecodeError, KeyError):
                pass

        t0 = time.perf_counter()
        stats = CollectStats(date=date_str)
        profiles: list[dict[str, Any]] = []

        for i, loc in enumerate(locations, 1):
            logger.info(
                "[%s] Profiling station %s (%d/%d) at (%.4f, %.4f)",
                date_str,
                loc.station_id,
                i,
                len(locations),
                loc.lat,
                loc.lon,
            )
            try:
                result = self._profile_station(loc, date_str)
                profiles.append(result)
                stats.locations_profiled += 1
            except Exception:
                logger.exception(
                    "Failed to profile station %s",
                    loc.station_id,
                )
                stats.locations_failed += 1

        stats.elapsed_s = round(time.perf_counter() - t0, 2)

        if save_local:
            self._save(day_dir, profiles, stats, locations)

        # Persist to TimescaleDB (optional)
        if persist_to_db and profiles:
            logger.warning(
                "Station-level Earth Engine database persistence is retired; "
                "skipping DB flush for %d station profiles.",
                len(profiles),
            )

        return stats

    def _save(
        self,
        day_dir: Path,
        profiles: list[dict[str, Any]],
        stats: CollectStats,
        locations: list[_LocationSpec],
    ) -> None:
        """Persist profiles and metadata to disk."""
        day_dir.mkdir(parents=True, exist_ok=True)

        # --- profiles.json ---
        profiles_file = day_dir / "profiles.json"
        with profiles_file.open("w", encoding="utf-8") as fh:
            json.dump(profiles, fh, indent=2, default=str)

        # --- metadata.json ---
        metadata = {
            "source": "Google Earth Engine",
            "version": "1.0",
            "data_range": {"date": stats.date},
            "sections_queried": self.sections,
            "results": {
                "locations_queried": len(locations),
                "locations_profiled": stats.locations_profiled,
                "locations_skipped": stats.locations_skipped,
                "locations_failed": stats.locations_failed,
                "location_sources": list({loc.source for loc in locations}),
            },
            "run": {
                "completed": True,
                "elapsed_s": stats.elapsed_s,
                "ingested_at": datetime.now(UTC).isoformat(),
                "parallel": self.parallel,
            },
        }
        meta_file = day_dir / "metadata.json"
        with meta_file.open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2, default=str)

        logger.info("Saved %d profiles → %s", len(profiles), day_dir)

    # ------------------------------------------------------------------ #
    #  Date-range collection                                              #
    # ------------------------------------------------------------------ #

    def collect_date_range(
        self,
        start_date: date_type,
        end_date: date_type,
        locations: list[_LocationSpec] | None = None,
        *,
        target: str = "local",
        force: bool = False,
    ) -> list[CollectStats]:
        """Profile locations for every day in [start_date, end_date].

        Args:
            start_date: First date (inclusive).
            end_date:   Last date (inclusive).
            locations:  Coordinates to profile.  If *None*, discovers
                        BirdWeather stations automatically.
            target:     Storage target — ``"local"`` (default) or
                        ``"database"``.
            force:      Re-run even if output already exists.

        Returns:
            List of per-day :class:`CollectStats`.
        """
        if locations is None:
            locations = self.discover_locations()
            if not locations:
                logger.error(
                    "No locations found.  Ensure BirdWeather has run, "
                    "the database is reachable, or supply --locations-file."
                )
                return []

        all_stats: list[CollectStats] = []
        current = start_date
        while current <= end_date:
            stats = self.collect_for_date(
                current,
                locations,
                target=target,
                force=force,
            )
            all_stats.append(stats)
            current += timedelta(days=1)

        total_profiled = sum(s.locations_profiled for s in all_stats)
        total_failed = sum(s.locations_failed for s in all_stats)
        logger.info(
            "Earth Engine complete: %d days, %d profiles, %d failures",
            len(all_stats),
            total_profiled,
            total_failed,
        )
        return all_stats


class EarthEngineGridCollector(EarthEngineCollector):
    """Collect Earth Engine profiles on a clipped geodesic grid over NL."""

    GRID_SYSTEM_PREFIX = "nl_geodesic"
    COUNTRY_WFS_URL = _PDOK_COUNTRY_WFS_URL
    _country_boundary_cache: list[_BoundaryPolygon] | None = None

    def __init__(
        self,
        output_dir: Path | None = None,
        project: str | None = None,
        high_volume: bool = False,
        parallel: bool = True,
        max_workers: int = DEFAULT_EE_SECTION_WORKERS,
        sections: list[str] | None = None,
        grid_size_km: float = DEFAULT_EE_GRID_SIZE_KM,
        cell_workers: int = DEFAULT_EE_GRID_CELL_WORKERS,
        db_batch_size: int = DEFAULT_EE_GRID_DB_BATCH_SIZE,
    ) -> None:
        super().__init__(
            output_dir=output_dir or _EE_GRID_ROOT,
            project=project,
            high_volume=high_volume,
            parallel=parallel,
            max_workers=max_workers,
            sections=sections,
        )
        self.grid_size_km = grid_size_km
        self.grid_system = self.grid_system_name(grid_size_km)
        self.cell_workers = max(1, int(cell_workers))
        self.db_batch_size = max(1, int(db_batch_size))
        if self.parallel and self.cell_workers > 1:
            section_cap = max(1, DEFAULT_EE_HTTP_POOL_SIZE // self.cell_workers)
            if self.max_workers > section_cap:
                logger.info(
                    "Capping Earth Engine section workers from %d to %d "
                    "for %d grid-cell workers to stay within HTTP pool size %d.",
                    self.max_workers,
                    section_cap,
                    self.cell_workers,
                    DEFAULT_EE_HTTP_POOL_SIZE,
                )
                self.max_workers = section_cap

    @staticmethod
    def _grid_size_token(grid_size_km: float) -> str:
        return f"{grid_size_km:g}".replace(".", "p")

    @classmethod
    def grid_system_name(cls, grid_size_km: float) -> str:
        return f"{cls.GRID_SYSTEM_PREFIX}_{cls._grid_size_token(grid_size_km)}km"

    @classmethod
    def grid_cell_id(cls, grid_size_km: float, row: int, col: int) -> str:
        return f"nl-{cls._grid_size_token(grid_size_km)}km-r{row:03d}-c{col:03d}"

    @staticmethod
    def _close_ring(
        ring: list[tuple[float, float]],
    ) -> tuple[tuple[float, float], ...]:
        if not ring:
            return ()
        if ring[0] != ring[-1]:
            ring = [*ring, ring[0]]
        return tuple(ring)

    @classmethod
    def _boundary_polygons_from_geometry(
        cls,
        geometry: dict[str, Any],
    ) -> list[_BoundaryPolygon]:
        polygons: list[_BoundaryPolygon] = []

        def _append_polygon(raw_rings: Any) -> None:
            if not isinstance(raw_rings, list) or not raw_rings:
                return

            rings: list[tuple[tuple[float, float], ...]] = []
            for raw_ring in raw_rings:
                if not isinstance(raw_ring, list):
                    continue
                ring = [
                    (float(point[0]), float(point[1]))
                    for point in raw_ring
                    if isinstance(point, list) and len(point) >= 2
                ]
                closed_ring = cls._close_ring(ring)
                if len(closed_ring) >= 4:
                    rings.append(closed_ring)

            if not rings:
                return

            outer_ring = rings[0]
            outer_points = outer_ring[:-1]
            lons = [point[0] for point in outer_points]
            lats = [point[1] for point in outer_points]
            polygons.append(
                _BoundaryPolygon(
                    outer_ring=outer_ring,
                    holes=tuple(rings[1:]),
                    bbox_min_lat=min(lats),
                    bbox_min_lon=min(lons),
                    bbox_max_lat=max(lats),
                    bbox_max_lon=max(lons),
                )
            )

        geom_type = geometry.get("type")
        if geom_type == "FeatureCollection":
            for feature in geometry.get("features", []):
                if isinstance(feature, dict):
                    polygons.extend(cls._boundary_polygons_from_geometry(feature))
            return polygons

        if geom_type == "Feature":
            nested = geometry.get("geometry")
            if isinstance(nested, dict):
                return cls._boundary_polygons_from_geometry(nested)
            return polygons

        coordinates = geometry.get("coordinates")
        if geom_type == "Polygon" and isinstance(coordinates, list):
            _append_polygon(coordinates)
        elif geom_type == "MultiPolygon" and isinstance(coordinates, list):
            for raw_polygon in coordinates:
                _append_polygon(raw_polygon)

        return polygons

    @classmethod
    def _load_country_boundary_polygons(cls) -> list[_BoundaryPolygon]:
        if cls._country_boundary_cache is not None:
            return cls._country_boundary_cache

        import requests as _requests

        try:
            response = _requests.get(
                cls.COUNTRY_WFS_URL,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "avian-biosurveillance-ingestion/0.1",
                },
                timeout=30,
            )
            response.raise_for_status()
            polygons = cls._boundary_polygons_from_geometry(response.json())
            if polygons:
                cls._country_boundary_cache = polygons
                return polygons
        except Exception:
            logger.warning(
                "Falling back to bounding-box Earth Engine grid discovery; "
                "failed to fetch Netherlands boundary clip.",
                exc_info=True,
            )

        return []

    @staticmethod
    def _point_on_segment(
        point: tuple[float, float],
        start: tuple[float, float],
        end: tuple[float, float],
    ) -> bool:
        px, py = point
        x1, y1 = start
        x2, y2 = end
        if not (
            min(x1, x2) - 1e-12 <= px <= max(x1, x2) + 1e-12
            and min(y1, y2) - 1e-12 <= py <= max(y1, y2) + 1e-12
        ):
            return False

        cross = (px - x1) * (y2 - y1) - (py - y1) * (x2 - x1)
        return abs(cross) <= 1e-12

    @classmethod
    def _point_in_ring(
        cls,
        point: tuple[float, float],
        ring: tuple[tuple[float, float], ...],
        *,
        include_boundary: bool,
    ) -> bool:
        px, py = point
        inside = False

        for idx in range(len(ring) - 1):
            start = ring[idx]
            end = ring[idx + 1]
            if cls._point_on_segment(point, start, end):
                return include_boundary

            x1, y1 = start
            x2, y2 = end
            if ((y1 > py) != (y2 > py)) and (
                px < (x2 - x1) * (py - y1) / (y2 - y1) + x1
            ):
                inside = not inside

        return inside

    @classmethod
    def _point_in_polygon(
        cls,
        point: tuple[float, float],
        polygon: _BoundaryPolygon,
        *,
        include_boundary: bool,
    ) -> bool:
        if not cls._point_in_ring(
            point,
            polygon.outer_ring,
            include_boundary=include_boundary,
        ):
            return False

        for hole in polygon.holes:
            if cls._point_in_ring(
                point,
                hole,
                include_boundary=not include_boundary,
            ):
                return False

        return True

    @staticmethod
    def _orientation(
        a: tuple[float, float],
        b: tuple[float, float],
        c: tuple[float, float],
    ) -> float:
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    @classmethod
    def _segments_cross(
        cls,
        a1: tuple[float, float],
        a2: tuple[float, float],
        b1: tuple[float, float],
        b2: tuple[float, float],
    ) -> bool:
        if (
            max(a1[0], a2[0]) < min(b1[0], b2[0])
            or max(b1[0], b2[0]) < min(a1[0], a2[0])
            or max(a1[1], a2[1]) < min(b1[1], b2[1])
            or max(b1[1], b2[1]) < min(a1[1], a2[1])
        ):
            return False

        o1 = cls._orientation(a1, a2, b1)
        o2 = cls._orientation(a1, a2, b2)
        o3 = cls._orientation(b1, b2, a1)
        o4 = cls._orientation(b1, b2, a2)
        eps = 1e-12
        return ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and (
            (o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)
        )

    @classmethod
    def _ring_has_proper_intersection(
        cls,
        cell_ring: tuple[tuple[float, float], ...],
        boundary_ring: tuple[tuple[float, float], ...],
    ) -> bool:
        for cell_idx in range(len(cell_ring) - 1):
            for boundary_idx in range(len(boundary_ring) - 1):
                if cls._segments_cross(
                    cell_ring[cell_idx],
                    cell_ring[cell_idx + 1],
                    boundary_ring[boundary_idx],
                    boundary_ring[boundary_idx + 1],
                ):
                    return True
        return False

    @classmethod
    def _cell_overlaps_country(
        cls,
        cell_geojson: dict[str, Any],
        polygons: list[_BoundaryPolygon],
    ) -> bool:
        coordinates = cell_geojson.get("coordinates")
        if not isinstance(coordinates, list) or not coordinates:
            return False

        cell_ring = cls._close_ring(
            [
                (float(point[0]), float(point[1]))
                for point in coordinates[0]
                if isinstance(point, list) and len(point) >= 2
            ]
        )
        if len(cell_ring) < 4:
            return False

        cell_vertices = cell_ring[:-1]
        cell_lons = [point[0] for point in cell_vertices]
        cell_lats = [point[1] for point in cell_vertices]
        cell_bbox_min_lon = min(cell_lons)
        cell_bbox_max_lon = max(cell_lons)
        cell_bbox_min_lat = min(cell_lats)
        cell_bbox_max_lat = max(cell_lats)
        centroid = (
            sum(cell_lons) / len(cell_lons),
            sum(cell_lats) / len(cell_lats),
        )

        for polygon in polygons:
            if (
                polygon.bbox_max_lat <= cell_bbox_min_lat
                or polygon.bbox_min_lat >= cell_bbox_max_lat
                or polygon.bbox_max_lon <= cell_bbox_min_lon
                or polygon.bbox_min_lon >= cell_bbox_max_lon
            ):
                continue

            if cls._point_in_polygon(
                centroid,
                polygon,
                include_boundary=True,
            ):
                return True

            if any(
                cls._point_in_polygon(
                    vertex,
                    polygon,
                    include_boundary=False,
                )
                for vertex in cell_vertices
            ):
                return True

            if any(
                cls._point_in_ring(
                    vertex,
                    cell_ring,
                    include_boundary=False,
                )
                for vertex in polygon.outer_ring[:-1]
            ):
                return True

            if cls._ring_has_proper_intersection(cell_ring, polygon.outer_ring):
                return True

            if any(
                cls._ring_has_proper_intersection(cell_ring, hole)
                for hole in polygon.holes
            ):
                return True

        return False

    @staticmethod
    def _move_point(
        lat: float, lon: float, bearing: float, km: float
    ) -> tuple[float, float]:
        """Move a WGS84 point geodesically by ``km`` along ``bearing`` degrees."""
        point = geodesic_distance(kilometers=km).destination((lat, lon), bearing)
        return float(point.latitude), float(point.longitude)

    @classmethod
    def _build_row_nodes(
        cls,
        south_origin: tuple[float, float],
        north_origin: tuple[float, float],
        grid_size_km: float,
        east_bound_lon: float,
    ) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        """Build paired south/north row nodes that share column indices.

        The previous implementation derived each cell's north-east corner from
        a different geodesic path than the next cell's north-west corner. On a
        sphere, ``north -> east`` is not identical to ``east -> north``, which
        produced subtly mismatched vertices and visibly misaligned lines.

        Building both row boundaries from shared column nodes guarantees that
        adjacent cells reuse the exact same vertices.
        """
        south_nodes: list[tuple[float, float]] = []
        north_nodes: list[tuple[float, float]] = []

        col = 0
        while True:
            south_node = cls._move_point(
                south_origin[0],
                south_origin[1],
                90,
                col * grid_size_km,
            )
            north_node = cls._move_point(
                north_origin[0],
                north_origin[1],
                90,
                col * grid_size_km,
            )
            south_nodes.append(south_node)
            north_nodes.append(north_node)

            if max(south_node[1], north_node[1]) >= east_bound_lon:
                break
            col += 1

        return south_nodes, north_nodes

    @classmethod
    def discover_grid_cells(
        cls,
        grid_size_km: float = DEFAULT_EE_GRID_SIZE_KM,
        *,
        ne: dict[str, float] | None = None,
        sw: dict[str, float] | None = None,
        country_geometry: dict[str, Any] | None = None,
    ) -> list[_GridCellSpec]:
        """Build geodesic grid cells over the Netherlands base grid."""
        if grid_size_km <= 0:
            raise ValueError("grid_size_km must be positive")

        ne_bounds = ne or EarthEngineCollector._NL_NE
        sw_bounds = sw or EarthEngineCollector._NL_SW
        if country_geometry is not None:
            country_polygons = cls._boundary_polygons_from_geometry(
                country_geometry,
            )
            if not country_polygons:
                raise ValueError(
                    "country_geometry must contain at least one Polygon or MultiPolygon"
                )
        elif ne is None and sw is None:
            country_polygons = cls._load_country_boundary_polygons()
        else:
            country_polygons = []

        cells: list[_GridCellSpec] = []
        grid_system = cls.grid_system_name(grid_size_km)

        row = 0
        while True:
            south_origin = cls._move_point(
                sw_bounds["lat"],
                sw_bounds["lon"],
                0,
                row * grid_size_km,
            )
            north_origin = cls._move_point(
                sw_bounds["lat"],
                sw_bounds["lon"],
                0,
                (row + 1) * grid_size_km,
            )

            south_nodes, north_nodes = cls._build_row_nodes(
                south_origin,
                north_origin,
                grid_size_km,
                ne_bounds["lon"],
            )

            for col in range(len(south_nodes) - 1):
                south_west_lat, south_west_lon = south_nodes[col]
                south_east_lat, south_east_lon = south_nodes[col + 1]
                north_west_lat, north_west_lon = north_nodes[col]
                north_east_lat, north_east_lon = north_nodes[col + 1]

                centroid_lat = (
                    south_west_lat + south_east_lat + north_east_lat + north_west_lat
                ) / 4
                centroid_lon = (
                    south_west_lon + south_east_lon + north_east_lon + north_west_lon
                ) / 4

                bbox_min_lat = min(
                    south_west_lat,
                    south_east_lat,
                    north_east_lat,
                    north_west_lat,
                )
                bbox_max_lat = max(
                    south_west_lat,
                    south_east_lat,
                    north_east_lat,
                    north_west_lat,
                )
                bbox_min_lon = min(
                    south_west_lon,
                    south_east_lon,
                    north_east_lon,
                    north_west_lon,
                )
                bbox_max_lon = max(
                    south_west_lon,
                    south_east_lon,
                    north_east_lon,
                    north_west_lon,
                )

                overlaps_bbox = not (
                    bbox_max_lat <= sw_bounds["lat"]
                    or bbox_min_lat >= ne_bounds["lat"]
                    or bbox_max_lon <= sw_bounds["lon"]
                    or bbox_min_lon >= ne_bounds["lon"]
                )
                if overlaps_bbox:
                    cell_geojson = {
                        "type": "Polygon",
                        "coordinates": [
                            [
                                [south_west_lon, south_west_lat],
                                [south_east_lon, south_east_lat],
                                [north_east_lon, north_east_lat],
                                [north_west_lon, north_west_lat],
                                [south_west_lon, south_west_lat],
                            ]
                        ],
                    }
                    if country_polygons and not cls._cell_overlaps_country(
                        cell_geojson,
                        country_polygons,
                    ):
                        continue

                    cells.append(
                        _GridCellSpec(
                            grid_cell_id=cls.grid_cell_id(grid_size_km, row, col),
                            grid_system=grid_system,
                            source="netherlands_geodesic_grid",
                            lat=centroid_lat,
                            lon=centroid_lon,
                            bbox_min_lat=bbox_min_lat,
                            bbox_min_lon=bbox_min_lon,
                            bbox_max_lat=bbox_max_lat,
                            bbox_max_lon=bbox_max_lon,
                            grid_size_km=grid_size_km,
                            cell_geojson=cell_geojson,
                            grid_row=row,
                            grid_col=col,
                        )
                    )

            if max(node[0] for node in north_nodes) >= ne_bounds["lat"]:
                break
            row += 1

        logger.info(
            "Generated %d Earth Engine grid cells at %.1f km resolution",
            len(cells),
            grid_size_km,
        )
        return cells

    def _profile_grid_cell(
        self,
        cell: _GridCellSpec,
        date_str: str,
    ) -> dict[str, Any]:
        """Query GEE for a single grid cell over its full polygon footprint."""
        profile = super()._profile_station(
            _LocationSpec(
                station_id=cell.grid_cell_id,
                source=cell.source,
                lat=cell.lat,
                lon=cell.lon,
            ),
            date_str,
            geometry=cell.cell_geojson,
        )
        profile["grid_cell_id"] = profile.pop("station_id")
        profile["bbox_min_lat"] = cell.bbox_min_lat
        profile["bbox_min_lon"] = cell.bbox_min_lon
        profile["bbox_max_lat"] = cell.bbox_max_lat
        profile["bbox_max_lon"] = cell.bbox_max_lon
        profile["grid_size_km"] = cell.grid_size_km
        return profile

    def _flush_grid_profiles_to_db(
        self,
        profiles: list[dict[str, Any]],
        target_date: date_type,
    ) -> int:
        """Persist a batch of grid profiles so long runs surface partial results."""
        if not profiles:
            return 0

        try:
            from avian_biosurveillance.db.loaders.earth_engine import (
                upsert_earth_engine_grid_profiles,
            )
            from avian_biosurveillance.db.models.earth_engine import (
                EarthEngineGridProfile,
            )

            db_records = [
                EarthEngineGridProfile.from_profile_dict(
                    profile,
                    grid_cell_id=profile.get("grid_cell_id", "unknown"),
                    observation_date=target_date,
                    source=profile.get("source"),
                )
                for profile in profiles
            ]
            inserted = upsert_earth_engine_grid_profiles(db_records)
            logger.info(
                "DB: upserted %d earth_engine grid profiles (batch)",
                inserted,
            )
            return inserted
        except Exception:
            logger.exception("DB upsert for earth_engine grid profiles failed")
            return 0

    def _ensure_grid_cells_registered(
        self,
        cells: list[_GridCellSpec],
    ) -> int:
        """Persist canonical grid-cell definitions before metric rows reference them."""
        if not cells:
            return 0

        try:
            from avian_biosurveillance.db.loaders.grid import upsert_grid_cells
            from avian_biosurveillance.db.models.grid import GridCell

            grid_records = [
                GridCell.from_spec(
                    grid_cell_id=cell.grid_cell_id,
                    grid_system=cell.grid_system,
                    source=cell.source,
                    lat=cell.lat,
                    lon=cell.lon,
                    bbox_min_lat=cell.bbox_min_lat,
                    bbox_min_lon=cell.bbox_min_lon,
                    bbox_max_lat=cell.bbox_max_lat,
                    bbox_max_lon=cell.bbox_max_lon,
                    grid_size_km=cell.grid_size_km,
                    cell_geojson=cell.cell_geojson,
                    grid_row=cell.grid_row,
                    grid_col=cell.grid_col,
                )
                for cell in cells
            ]
            inserted = upsert_grid_cells(grid_records)
            logger.info("DB: upserted %d canonical grid cells", inserted)
            return inserted
        except Exception:
            logger.exception("DB upsert for canonical grid cells failed")
            return 0

    @staticmethod
    def _coerce_grid_cells(
        locations: list[_LocationSpec] | list[_GridCellSpec] | None,
    ) -> list[_GridCellSpec] | None:
        """Accept only grid-cell specs while keeping a compatible method signature."""
        if locations is None:
            return None
        if all(isinstance(location, _GridCellSpec) for location in locations):
            return [
                location
                for location in locations
                if isinstance(location, _GridCellSpec)
            ]
        raise TypeError(
            "EarthEngineGridCollector expects grid-cell specs; "
            "use EarthEngineCollector for station locations."
        )

    @staticmethod
    def _grid_cell_ids(cells: list[_GridCellSpec]) -> list[str]:
        """Return stable grid-cell ids for DB coverage checks."""
        return [cell.grid_cell_id for cell in cells]

    def _existing_grid_profile_counts_by_date(
        self,
        *,
        start_date: date_type,
        end_date: date_type,
        cells: list[_GridCellSpec],
    ) -> dict[date_type, int]:
        """Return existing DB row counts for each requested date."""
        grid_cell_ids = self._grid_cell_ids(cells)
        if not grid_cell_ids:
            return {}

        try:
            from avian_biosurveillance.db.database import get_pool

            pool = get_pool()
            with pool.cursor() as cur:
                cur.execute(
                    """
                    SELECT observation_date::date, COUNT(*)::int
                    FROM google_earth_engine_tbl
                    WHERE observation_date >= %s::date
                      AND observation_date <= %s::date
                      AND grid_cell_id = ANY(%s)
                    GROUP BY observation_date
                    """,
                    (start_date, end_date, grid_cell_ids),
                )
                rows = cur.fetchall()
        except Exception:
            logger.warning(
                "Could not inspect existing Earth Engine grid coverage; "
                "continuing without DB date skips.",
                exc_info=True,
            )
            return {}

        out: dict[date_type, int] = {}
        for row in rows:
            day = row[0]
            if isinstance(day, datetime):
                day = day.date()
            elif not isinstance(day, date_type):
                day = _parse_date(str(day)).date()
            out[day] = int(row[1] or 0)
        return out

    def _existing_grid_profile_ids_for_date(
        self,
        target_date: date_type,
        cells: list[_GridCellSpec],
    ) -> set[str]:
        """Return grid cells that already have a DB profile for one date."""
        grid_cell_ids = self._grid_cell_ids(cells)
        if not grid_cell_ids:
            return set()

        try:
            from avian_biosurveillance.db.database import get_pool

            pool = get_pool()
            with pool.cursor() as cur:
                cur.execute(
                    """
                    SELECT grid_cell_id
                    FROM google_earth_engine_tbl
                    WHERE observation_date = %s::date
                      AND grid_cell_id = ANY(%s)
                    """,
                    (target_date, grid_cell_ids),
                )
                rows = cur.fetchall()
        except Exception:
            logger.warning(
                "Could not inspect existing Earth Engine grid rows for %s; "
                "continuing without DB cell skips.",
                target_date,
                exc_info=True,
            )
            return set()

        return {str(row[0]) for row in rows}

    def collect_for_date(
        self,
        target_date: date_type,
        locations: list[_LocationSpec] | list[_GridCellSpec] | None = None,
        *,
        target: str = "local",
        force: bool = False,
    ) -> CollectStats:
        """Profile all grid cells for a single date."""
        persist_to_db = target == "database"
        if persist_to_db and set(self.sections) != set(self._SECTIONS):
            msg = (
                "Database target requires a full Earth Engine profile. "
                "Partial section runs are only supported for local output "
                "because omitted sections would overwrite existing DB values."
            )
            raise ValueError(msg)

        save_local = target == "local"
        cells = self._coerce_grid_cells(locations) or self.discover_grid_cells(
            self.grid_size_km
        )
        date_str = target_date.isoformat()
        day_dir = Path(self.output_dir) / date_str
        meta_file = day_dir / "metadata.json"

        if save_local and not force and meta_file.exists():
            try:
                prev = json.loads(meta_file.read_text(encoding="utf-8"))
                if prev.get("run", {}).get("completed"):
                    logger.info(
                        "Skipping %s — grid run already complete (use --force to re-run)",
                        date_str,
                    )
                    return CollectStats(date=date_str)
            except (json.JSONDecodeError, KeyError):
                pass

        t0 = time.perf_counter()
        stats = CollectStats(date=date_str)
        cells_to_profile = cells

        if persist_to_db and not force:
            existing_ids = self._existing_grid_profile_ids_for_date(
                target_date,
                cells,
            )
            if existing_ids:
                cells_to_profile = [
                    cell for cell in cells if cell.grid_cell_id not in existing_ids
                ]
                stats.locations_skipped = len(cells) - len(cells_to_profile)
                if not cells_to_profile:
                    stats.elapsed_s = round(time.perf_counter() - t0, 2)
                    logger.info(
                        "Skipping %s — database already has %d/%d grid profiles.",
                        date_str,
                        stats.locations_skipped,
                        len(cells),
                    )
                    return stats
                logger.info(
                    "[%s] Skipping %d existing DB grid profile(s); profiling %d missing.",
                    date_str,
                    stats.locations_skipped,
                    len(cells_to_profile),
                )

        indexed_profiles: list[tuple[int, dict[str, Any]]] = []
        pending_db_profiles: list[dict[str, Any]] = []

        if persist_to_db:
            self._ensure_grid_cells_registered(cells)

        def _record_profile(index: int, profile: dict[str, Any]) -> None:
            indexed_profiles.append((index, profile))
            pending_db_profiles.append(profile)
            stats.locations_profiled += 1
            if persist_to_db and len(pending_db_profiles) >= self.db_batch_size:
                self._flush_grid_profiles_to_db(
                    pending_db_profiles,
                    target_date,
                )
                pending_db_profiles.clear()

        if self.cell_workers > 1 and len(cells_to_profile) > 1:
            with ThreadPoolExecutor(max_workers=self.cell_workers) as pool:
                futures = {}
                for i, cell in enumerate(cells_to_profile, 1):
                    logger.info(
                        "[%s] Profiling grid cell %s (%d/%d) at (%.4f, %.4f)",
                        date_str,
                        cell.grid_cell_id,
                        i,
                        len(cells_to_profile),
                        cell.lat,
                        cell.lon,
                    )
                    futures[pool.submit(self._profile_grid_cell, cell, date_str)] = (
                        i,
                        cell,
                    )

                for future in as_completed(futures):
                    i, cell = futures[future]
                    try:
                        result = future.result()
                    except Exception:
                        logger.exception(
                            "Failed to profile grid cell %s",
                            cell.grid_cell_id,
                        )
                        stats.locations_failed += 1
                    else:
                        _record_profile(i, result)
        else:
            for i, cell in enumerate(cells_to_profile, 1):
                logger.info(
                    "[%s] Profiling grid cell %s (%d/%d) at (%.4f, %.4f)",
                    date_str,
                    cell.grid_cell_id,
                    i,
                    len(cells_to_profile),
                    cell.lat,
                    cell.lon,
                )
                try:
                    result = self._profile_grid_cell(cell, date_str)
                except Exception:
                    logger.exception("Failed to profile grid cell %s", cell.grid_cell_id)
                    stats.locations_failed += 1
                else:
                    _record_profile(i, result)

        stats.elapsed_s = round(time.perf_counter() - t0, 2)
        profiles = [profile for _, profile in sorted(indexed_profiles)]
        if save_local:
            self._save_grid(day_dir, profiles, stats, cells)

        if persist_to_db and pending_db_profiles:
            self._flush_grid_profiles_to_db(pending_db_profiles, target_date)

        return stats

    def _save_grid(
        self,
        day_dir: Path,
        profiles: list[dict[str, Any]],
        stats: CollectStats,
        grid_cells: list[_GridCellSpec],
    ) -> None:
        """Persist grid-cell profiles and metadata to disk."""
        day_dir.mkdir(parents=True, exist_ok=True)

        profiles_file = day_dir / "profiles.json"
        with profiles_file.open("w", encoding="utf-8") as fh:
            json.dump(profiles, fh, indent=2, default=str)

        metadata = {
            "source": "Google Earth Engine",
            "version": "1.0",
            "collection_type": "grid",
            "data_range": {"date": stats.date},
            "sections_queried": self.sections,
            "grid": {
                "grid_size_km": self.grid_size_km,
                "bbox": {
                    "sw": dict(self._NL_SW),
                    "ne": dict(self._NL_NE),
                },
            },
            "results": {
                "grid_cells_queried": len(grid_cells),
                "grid_cells_profiled": stats.locations_profiled,
                "grid_cells_skipped": stats.locations_skipped,
                "grid_cells_failed": stats.locations_failed,
                "location_sources": list({cell.source for cell in grid_cells}),
            },
            "run": {
                "completed": True,
                "elapsed_s": stats.elapsed_s,
                "ingested_at": datetime.now(UTC).isoformat(),
                "parallel": self.parallel,
            },
        }
        meta_file = day_dir / "metadata.json"
        with meta_file.open("w", encoding="utf-8") as fh:
            json.dump(metadata, fh, indent=2, default=str)

        logger.info("Saved %d grid profiles → %s", len(profiles), day_dir)

    def collect_date_range(
        self,
        start_date: date_type,
        end_date: date_type,
        locations: list[_LocationSpec] | list[_GridCellSpec] | None = None,
        *,
        target: str = "local",
        force: bool = False,
        only_dates: Iterable[date_type] | None = None,
    ) -> list[CollectStats]:
        """Profile grid cells for days in [start_date, end_date].

        When *only_dates* is provided, only those calendar days within the range
        are profiled (e.g. bi-weekly anchor dates); all other days are skipped.
        """
        restrict_dates: set[date_type] | None = (
            set(only_dates) if only_dates is not None else None
        )
        cells = self._coerce_grid_cells(locations) or self.discover_grid_cells(
            self.grid_size_km
        )
        if not cells:
            logger.error("No grid cells generated for Earth Engine collection")
            return []

        complete_db_dates: set[date_type] = set()
        if target == "database" and not force:
            existing_counts = self._existing_grid_profile_counts_by_date(
                start_date=start_date,
                end_date=end_date,
                cells=cells,
            )
            complete_db_dates = {
                day for day, row_count in existing_counts.items()
                if row_count >= len(cells)
            }
            if complete_db_dates:
                logger.info(
                    "Skipping %d Earth Engine grid day(s) already complete in DB.",
                    len(complete_db_dates),
                )

        all_stats: list[CollectStats] = []
        current = start_date
        while current <= end_date:
            if restrict_dates is not None and current not in restrict_dates:
                current += timedelta(days=1)
                continue

            if current in complete_db_dates:
                all_stats.append(
                    CollectStats(
                        date=current.isoformat(),
                        locations_skipped=len(cells),
                    )
                )
                current += timedelta(days=1)
                continue

            stats = self.collect_for_date(
                current,
                cells,
                target=target,
                force=force,
            )
            all_stats.append(stats)
            current += timedelta(days=1)

        total_profiled = sum(s.locations_profiled for s in all_stats)
        total_skipped = sum(s.locations_skipped for s in all_stats)
        total_failed = sum(s.locations_failed for s in all_stats)
        logger.info(
            "Earth Engine grid complete: %d days, %d profiles, %d skipped, %d failures",
            len(all_stats),
            total_profiled,
            total_skipped,
            total_failed,
        )
        return all_stats


# ====================================================================== #
#  CLI                                                                    #
# ====================================================================== #


def _parse_cli_date(value: str) -> date_type:
    """Parse ``YYYY-MM-DD`` string into a :class:`date`."""
    return datetime.strptime(value, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> None:
    """CLI entry point for Earth Engine ingestion."""
    parser = argparse.ArgumentParser(
        description="Profile locations via Google Earth Engine.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    # Mode: date-range (batch) vs single-point
    mode_grp = parser.add_argument_group("collection mode")
    mode_grp.add_argument(
        "--start-date",
        type=_parse_cli_date,
        help="Start date (YYYY-MM-DD) for batch collection",
    )
    mode_grp.add_argument(
        "--end-date",
        type=_parse_cli_date,
        help="End date (YYYY-MM-DD) for batch collection",
    )
    mode_grp.add_argument(
        "--date",
        type=str,
        default=None,
        help="Single date for quick profiling (YYYY-MM-DD)",
    )
    mode_grp.add_argument(
        "--grid",
        action="store_true",
        help=(
            "Collect 5 km geodesic grid profiles over the Netherlands "
            "instead of station profiles"
        ),
    )

    # Location overrides
    loc_grp = parser.add_argument_group("location")
    loc_grp.add_argument("--lat", type=float, help="Latitude (single point)")
    loc_grp.add_argument("--lon", type=float, help="Longitude (single point)")
    loc_grp.add_argument(
        "--locations-file",
        type=Path,
        default=None,
        help="JSON/JSONL file with [{id, lat, lon}, ...]",
    )

    # GEE options
    gee_grp = parser.add_argument_group("GEE options")
    gee_grp.add_argument("--project", type=str, default=None, help="GEE project ID")
    gee_grp.add_argument(
        "--high-volume",
        action="store_true",
        help="Use the GEE high-volume endpoint",
    )
    gee_grp.add_argument(
        "--parallel",
        action="store_true",
        default=True,
        help="Profile sections in parallel (default: True)",
    )
    gee_grp.add_argument(
        "--no-parallel",
        action="store_true",
        help="Disable parallel profiling",
    )
    gee_grp.add_argument(
        "--max-workers",
        type=int,
        default=DEFAULT_EE_SECTION_WORKERS,
        help=(
            "Parallel section workers per location "
            f"(default: {DEFAULT_EE_SECTION_WORKERS})"
        ),
    )
    gee_grp.add_argument(
        "--section",
        type=str,
        default=None,
        help="Query a single section only (e.g. 'climate')",
    )
    gee_grp.add_argument(
        "--grid-size-km",
        type=float,
        default=5.0,
        help="Grid cell size in kilometres when using --grid (default: 5)",
    )
    gee_grp.add_argument(
        "--anchors-only",
        action="store_true",
        help=(
            "With --grid, only profile bi-weekly anchor dates (1st/15th) within "
            "the requested range, matching the production ingestion cadence."
        ),
    )
    gee_grp.add_argument(
        "--cell-workers",
        type=int,
        default=DEFAULT_EE_GRID_CELL_WORKERS,
        help=(
            "Parallel grid cells to profile when using --grid "
            f"(default: {DEFAULT_EE_GRID_CELL_WORKERS})"
        ),
    )
    gee_grp.add_argument(
        "--db-batch-size",
        type=int,
        default=DEFAULT_EE_GRID_DB_BATCH_SIZE,
        help=(
            "Rows per database upsert batch when using --grid "
            f"(default: {DEFAULT_EE_GRID_DB_BATCH_SIZE})"
        ),
    )

    # Output
    out_grp = parser.add_argument_group("output")
    out_grp.add_argument("--json", action="store_true", help="Print JSON to stdout")
    out_grp.add_argument(
        "--target",
        choices=["local", "database"],
        default="local",
        help="Storage target: local (disk only, default) or database (TimescaleDB only)",
    )
    out_grp.add_argument("--force", action="store_true", help="Re-run even if exists")

    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    parallel = not args.no_parallel
    sections = [args.section] if args.section else None

    if args.section and args.target == "database":
        parser.error(
            "Partial section runs cannot target the database; use --target "
            "local or omit --section."
        )

    # --- Single-point quick profile (original profiler behaviour) ---
    if args.lat is not None and args.lon is not None and args.date:
        _initialize_ee(project=args.project, high_volume=args.high_volume)
        if args.section:
            try:
                result = run_section(
                    args.section,
                    args.lat,
                    args.lon,
                    args.date,
                )
            except KeyError:
                parser.error(f"Unknown section: {args.section}")
                return  # pragma: no cover — parser.error raises
        else:
            result = run_all(args.lat, args.lon, args.date)
        if args.json:
            sys.stdout.write(json.dumps(result, indent=2, default=str) + "\n")
        else:
            EarthEngineProfiler.print_report(result)
        return

    # --- Batch collection (date range) ---
    if not args.start_date or not args.end_date:
        parser.error(
            "Provide --start-date + --end-date for batch collection, "
            "or --lat --lon --date for single-point profiling."
        )

    if args.grid:
        if args.locations_file:
            parser.error("--locations-file cannot be combined with --grid")
        grid_collector = EarthEngineGridCollector(
            project=args.project,
            high_volume=args.high_volume,
            parallel=parallel,
            max_workers=args.max_workers,
            sections=sections,
            grid_size_km=args.grid_size_km,
            cell_workers=args.cell_workers,
            db_batch_size=args.db_batch_size,
        )
        results = grid_collector.collect_date_range(
            start_date=args.start_date,
            end_date=args.end_date,
            target=args.target,
            force=args.force,
            only_dates=(
                anchor_dates_in_range(args.start_date, args.end_date)
                if args.anchors_only
                else None
            ),
        )
    else:
        station_collector = EarthEngineCollector(
            project=args.project,
            high_volume=args.high_volume,
            parallel=parallel,
            max_workers=args.max_workers,
            sections=sections,
        )

        locations: list[_LocationSpec] | None = None
        if args.locations_file:
            locations = EarthEngineCollector.load_locations_file(args.locations_file)

        results = station_collector.collect_date_range(
            start_date=args.start_date,
            end_date=args.end_date,
            locations=locations,
            target=args.target,
            force=args.force,
        )

    if args.json:
        sys.stdout.write(
            json.dumps(
                [
                    {
                        "date": s.date,
                        "profiled": s.locations_profiled,
                        "skipped": s.locations_skipped,
                        "failed": s.locations_failed,
                        "elapsed_s": s.elapsed_s,
                    }
                    for s in results
                ],
                indent=2,
            )
            + "\n"
        )


if __name__ == "__main__":
    main()
