from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

from avian_biosurveillance.db.loaders import cds_drought as cds_loader
from avian_biosurveillance.db.models import CDSDroughtObservation
from avian_biosurveillance.db.models.cds_drought import (
    build_cds_bbox,
    build_cds_grid_cell_id,
)


def _mock_pool() -> tuple[MagicMock, MagicMock]:
    pool = MagicMock()
    cursor = MagicMock()
    pool.cursor.return_value.__enter__ = MagicMock(return_value=cursor)
    pool.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return pool, cursor


def _sample_record(**overrides: object) -> CDSDroughtObservation:
    lat = float(overrides.pop("lat", 52.25))
    lon = float(overrides.pop("lon", 4.75))
    bbox_min_lat, bbox_min_lon, bbox_max_lat, bbox_max_lon = build_cds_bbox(lat, lon)
    payload: dict[str, object] = {
        "grid_cell_id": build_cds_grid_cell_id(lat, lon),
        "observation_month": date(2025, 1, 1),
        "dataset_type": "consolidated_dataset",
        "product_type": "reanalysis",
        "lat": lat,
        "lon": lon,
        "bbox_min_lat": bbox_min_lat,
        "bbox_min_lon": bbox_min_lon,
        "bbox_max_lat": bbox_max_lat,
        "bbox_max_lon": bbox_max_lon,
        "spi_1m": -1.25,
        "spei_1m": -1.5,
        "spi_p0_1m": 0.12,
        "spi_normality_1m": True,
        "spei_normality_1m": False,
    }
    payload.update(overrides)
    return CDSDroughtObservation(**payload)


def test_from_mapping_coerces_dates_and_defaults() -> None:
    record = CDSDroughtObservation.from_mapping(
        {
            "grid_cell_id": "cds_0.25deg_lat_52.25_lon_4.75",
            "observation_month": "2025-01-01",
            "dataset_type": "intermediate_dataset",
            "product_type": "ensemble_members",
            "ensemble_member": "3",
            "lat": 52.25,
            "lon": 4.75,
            "bbox_min_lat": 52.125,
            "bbox_min_lon": 4.625,
            "bbox_max_lat": 52.375,
            "bbox_max_lon": 4.875,
            "spi_1m": -0.75,
            "spi_normality_1m": True,
        }
    )

    assert record.observation_month == date(2025, 1, 1)
    assert record.ensemble_member == 3
    assert record.data_version == "1_0"
    assert record.spi_1m == -0.75
    assert record.spi_normality_1m is True


@patch("avian_biosurveillance.db.loaders.cds_drought.psycopg2.extras.execute_values")
def test_upsert_cds_drought_observations_uses_expected_columns(
    mock_exec: MagicMock,
) -> None:
    pool, _cursor = _mock_pool()
    record = _sample_record()

    total = cds_loader.upsert_cds_drought_observations([record], pool=pool)

    assert total == 1
    mock_exec.assert_called_once()
    sql = mock_exec.call_args[0][1]
    values = mock_exec.call_args[0][2]
    row = values[0]
    col_idx = {col: idx for idx, col in enumerate(cds_loader._CDS_COLUMNS)}

    assert "cds_drought_tbl" in sql
    assert (
        "ON CONFLICT (grid_cell_id, observation_month, dataset_type, "
        "product_type, ensemble_member, data_version)" in sql
    )
    assert row[col_idx["grid_cell_id"]] == build_cds_grid_cell_id(52.25, 4.75)
    assert row[col_idx["spi_1m"]] == -1.25
    assert row[col_idx["spei_1m"]] == -1.5
    assert row[col_idx["spi_p0_1m"]] == 0.12
    assert row[col_idx["spi_normality_1m"]] is True
    assert row[col_idx["spei_normality_1m"]] is False
