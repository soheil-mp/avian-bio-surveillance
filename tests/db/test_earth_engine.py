from __future__ import annotations

# mypy: disable-error-code=import-untyped,no-untyped-def
import json
from datetime import date
from typing import Any
from unittest.mock import MagicMock, patch

from avian_biosurveillance.db.loaders import earth_engine as ee_loader  # type: ignore
from avian_biosurveillance.db.loaders import grid as grid_loader
from avian_biosurveillance.db.models import (  # type: ignore
    EarthEngineGridProfile,
    EarthEngineProfile,
    GridCell,
)


def _mock_pool() -> tuple[MagicMock, MagicMock]:
    pool = MagicMock()
    cursor = MagicMock()
    pool.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    pool.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return pool, cursor


def _sample_profile() -> dict[str, Any]:
    return {
        "location": {
            "latitude": 52.123,
            "longitude": 5.456,
            "date": "2026-03-23",
            "grid_cell": "31UFT",
        },
        "elevation": {
            "elevation_m": 12.5,
            "slope_degrees": 2.5,
            "aspect_degrees": 180.0,
        },
        "land_cover": {
            "dynamic_world": {
                "value": 4,
                "label": "Cropland",
                "confidence": 0.91,
            },
            "esa_worldcover": {"value": 10, "label": "Tree cover"},
            "modis_igbp": {"value": 8, "label": "Woody savannas"},
        },
        "vegetation": {
            "ndvi": 0.64,
            "evi": 0.41,
            "lai": 1.9,
            "fpar": 0.73,
            "custom_index": 0.88,
        },
        "surface_temperature": {
            "lst_day_celsius": 24.2,
            "lst_night_celsius": 15.3,
        },
        "climate": {
            "temperature_2m_c": 18.4,
            "dewpoint_2m_c": 11.1,
            "total_precipitation_mm": 2.7,
            "snowfall_m": 0.0,
            "snow_depth_m": 0.0,
            "wind_u_10m_ms": 3.0,
            "wind_v_10m_ms": 4.0,
            "surface_pressure_pa": 101325.0,
            "soil_temp_level1_c": 16.2,
            "soil_moisture": 0.21,
            "soil_moisture_level2": 0.27,
            "surface_solar_rad_down_j_m2": 1234.0,
            "surface_thermal_rad_down_j_m2": 567.0,
            "total_evaporation_m": 0.003,
            "leaf_area_index_high": 2.1,
            "leaf_area_index_low": 1.1,
        },
        "air_quality": {
            "no2_mol_m2": 0.00012,
            "co_mol_m2": 0.012,
            "so2_mol_m2": 0.00003,
            "ozone_mol_m2": 0.004,
            "aerosol_index": 1.2,
        },
        "cloud_properties": {
            "cloud_fraction": 0.34,
            "cloud_top_height_m": 8100.0,
            "cloud_base_height_m": 1200.0,
            "cloud_optical_depth": 9.5,
        },
        "surface_water": {
            "surface_water_pct": 8.5,
            "surface_water_change_abs": -0.5,
            "surface_water_seasonality_months": 2.0,
            "surface_water_recurrence_pct": 91.0,
        },
        "forest": {
            "forest_cover_pct": 14.5,
            "forest_loss": False,
            "forest_gain": True,
            "forest_loss_year": None,
        },
        "nighttime_lights": {
            "nighttime_radiance_nw": 3.4,
            "cloud_free_observations": 12,
        },
        "population": {
            "worldpop_count": 127.0,
            "gpw_density_per_km2": 88.0,
        },
        "human_modification": {"human_modification_index": 0.42},
        "fire": {
            "burned_area_date": 20260310,
            "fire_brightness_kelvin": 317.5,
        },
        "soil": {
            "clay_pct": 22.0,
            "sand_pct": 38.0,
            "organic_carbon_g_kg": 15.0,
            "soil_ph": 6.8,
            "bulk_density_kg_m3": 1325.0,
        },
        "evapotranspiration": {"et_mm_8day": 14.0, "pet_mm_8day": 18.5},
        "sea_surface_temperature": {
            "sst_celsius": 12.1,
            "sst_anom_celsius": 0.6,
        },
        "_meta": {
            "mode": "parallel",
            "total_seconds": 12.4,
            "request_id": "req-123",
        },
        "debug": {"raw_source": "earth-engine"},
    }


def test_from_profile_dict_flattens_metrics_and_keeps_only_residual_json() -> None:
    profile = EarthEngineProfile.from_profile_dict(
        _sample_profile(),
        station_id="BW-001",
        observation_date=date(2026, 3, 23),
        source="birdweather_api",
    )

    assert profile.lat == 52.123
    assert profile.lon == 5.456
    assert profile.slope_degrees == 2.5
    assert profile.lai == 1.9
    assert profile.wind_speed_10m_ms == 5.0
    assert profile.wind_u_10m_ms == 3.0
    assert profile.wind_v_10m_ms == 4.0
    assert profile.cloud_cover_pct == 34.0
    assert profile.dynamic_world_label == "Cropland"
    assert profile.esa_worldcover_class == 10
    assert profile.nighttime_radiance_nw == 3.4
    assert profile.sst_anom_celsius == 0.6
    assert profile.profiler_mode == "parallel"
    assert profile.profiler_elapsed_s == 12.4

    residual = json.loads(profile.profile_data)
    availability = json.loads(profile.section_availability)
    assert residual == {
        "location": {"grid_cell": "31UFT"},
        "land_cover": {"dynamic_world": {"confidence": 0.91}},
        "vegetation": {"custom_index": 0.88},
        "_meta": {"request_id": "req-123"},
        "debug": {"raw_source": "earth-engine"},
    }
    assert availability["vegetation"]["status"] == "available"
    assert availability["forest"]["status"] == "partial"
    assert availability["population"]["status"] == "available"


@patch("avian_biosurveillance.db.loaders.earth_engine.psycopg2.extras.execute_values")
def test_upsert_earth_engine_profiles_is_retired(
    mock_exec: MagicMock,
) -> None:
    profile = EarthEngineProfile.from_profile_dict(
        _sample_profile(),
        station_id="BW-001",
        observation_date=date(2026, 3, 23),
        source="birdweather_api",
    )

    with pytest.raises(RuntimeError, match="retired"):
        ee_loader.upsert_earth_engine_profiles([profile])

    mock_exec.assert_not_called()


def test_from_profile_dict_normalizes_placeholder_land_cover_labels() -> None:
    profile_dict = _sample_profile()
    profile_dict["land_cover"] = {
        "dynamic_world": {"value": None, "label": "None"},
        "esa_worldcover": {"value": None, "label": "None"},
        "modis_igbp": {"value": None, "label": "None"},
    }

    profile = EarthEngineProfile.from_profile_dict(
        profile_dict,
        station_id="BW-001",
        observation_date=date(2026, 3, 23),
        source="birdweather_api",
    )

    assert profile.dynamic_world_label is None
    assert profile.esa_worldcover_label is None
    assert profile.modis_igbp_label is None
    assert json.loads(profile.section_availability)["land_cover"]["status"] == "missing"


def test_grid_profile_from_profile_dict_keeps_grid_metadata_flat() -> None:
    profile_dict = _sample_profile()
    profile_dict.update(
        {
            "grid_cell_id": "nl-5km-r000-c000",
        }
    )

    profile = EarthEngineGridProfile.from_profile_dict(
        profile_dict,
        grid_cell_id="nl-5km-r000-c000",
        observation_date=date(2026, 3, 23),
        source="netherlands_geodesic_grid",
    )

    assert profile.grid_cell_id == "nl-5km-r000-c000"
    assert profile.ndvi == 0.64
    assert profile.wind_speed_10m_ms == 5.0


def test_grid_cell_from_spec_serializes_polygon_geometry() -> None:
    cell = GridCell.from_spec(
        grid_cell_id="nl-5km-r000-c000",
        grid_system="nl_geodesic_5km",
        source="netherlands_geodesic_grid",
        lat=52.125,
        lon=5.435,
        bbox_min_lat=52.10,
        bbox_min_lon=5.40,
        bbox_max_lat=52.15,
        bbox_max_lon=5.47,
        grid_size_km=5.0,
        cell_geojson={
            "type": "Polygon",
            "coordinates": [
                [
                    [5.40, 52.10],
                    [5.47, 52.10],
                    [5.47, 52.15],
                    [5.40, 52.15],
                    [5.40, 52.10],
                ]
            ],
        },
    )

    assert cell.grid_system == "nl_geodesic_5km"
    assert json.loads(cell.cell_geojson)["type"] == "Polygon"


@patch("avian_biosurveillance.db.loaders.grid.psycopg2.extras.execute_values")
def test_upsert_grid_cells_uses_canonical_grid_columns(
    mock_exec: MagicMock,
) -> None:
    pool, _cursor = _mock_pool()
    cell = GridCell.from_spec(
        grid_cell_id="nl-5km-r000-c000",
        grid_system="nl_geodesic_5km",
        source="netherlands_geodesic_grid",
        lat=52.125,
        lon=5.435,
        bbox_min_lat=52.10,
        bbox_min_lon=5.40,
        bbox_max_lat=52.15,
        bbox_max_lon=5.47,
        grid_size_km=5.0,
        cell_geojson={
            "type": "Polygon",
            "coordinates": [
                [
                    [5.40, 52.10],
                    [5.47, 52.10],
                    [5.47, 52.15],
                    [5.40, 52.15],
                    [5.40, 52.10],
                ]
            ],
        },
    )

    total = grid_loader.upsert_grid_cells([cell], pool=pool)

    assert total == 1
    mock_exec.assert_called_once()
    sql = mock_exec.call_args[0][1]
    values = mock_exec.call_args[0][2]
    row = values[0]
    col_idx = {col: idx for idx, col in enumerate(grid_loader._GRID_COLUMNS)}

    assert "grid_cells_tbl" in sql
    assert row[col_idx["grid_cell_id"]] == "nl-5km-r000-c000"
    assert row[col_idx["grid_system"]] == "nl_geodesic_5km"
    assert json.loads(row[col_idx["cell_geojson"]])["type"] == "Polygon"


@patch("avian_biosurveillance.db.loaders.earth_engine.psycopg2.extras.execute_values")
def test_upsert_earth_engine_grid_profiles_uses_grid_columns(
    mock_exec: MagicMock,
) -> None:
    pool, _cursor = _mock_pool()
    profile = EarthEngineGridProfile.from_profile_dict(
        _sample_profile(),
        grid_cell_id="nl-5km-r000-c000",
        observation_date=date(2026, 3, 23),
        source="netherlands_geodesic_grid",
    )

    total = ee_loader.upsert_earth_engine_grid_profiles([profile], pool=pool)

    assert total == 1
    mock_exec.assert_called_once()
    sql = mock_exec.call_args[0][1]
    values = mock_exec.call_args[0][2]
    row = values[0]
    col_idx = {col: idx for idx, col in enumerate(ee_loader._EEGP_COLUMNS)}

    assert "google_earth_engine_tbl" in sql
    assert "grid_cell_id" in sql
    assert row[col_idx["grid_cell_id"]] == "nl-5km-r000-c000"
    assert row[col_idx["wind_speed_10m_ms"]] == 5.0
