"""Tests for CDSDroughtObservation.from_mapping() and cds_drought helpers."""

from __future__ import annotations

from datetime import date

import pytest

from avian_biosurveillance.db.models.cds_drought import (
    CDSDroughtObservation,
    build_cds_bbox,
    build_cds_grid_cell_id,
)


class TestBuildCdsGridCellId:
    def test_default_resolution(self):
        result = build_cds_grid_cell_id(52.25, 4.75)
        assert "52.25" in result
        assert "4.75" in result
        assert result.startswith("cds_")

    def test_custom_resolution(self):
        result = build_cds_grid_cell_id(52.0, 5.0, resolution_degrees=0.5)
        assert "0.5deg" in result


class TestBuildCdsBbox:
    def test_default_resolution(self):
        min_lat, min_lon, max_lat, max_lon = build_cds_bbox(52.25, 4.75)
        assert min_lat == pytest.approx(52.125)
        assert min_lon == pytest.approx(4.625)
        assert max_lat == pytest.approx(52.375)
        assert max_lon == pytest.approx(4.875)

    def test_custom_resolution(self):
        min_lat, min_lon, max_lat, max_lon = build_cds_bbox(
            52.0, 5.0, resolution_degrees=0.5
        )
        assert min_lat == pytest.approx(51.75)
        assert max_lat == pytest.approx(52.25)


class TestCDSDroughtObservationFromMapping:
    def _base(self, **overrides) -> dict:
        lat, lon = 52.25, 4.75
        base = {
            "grid_cell_id": build_cds_grid_cell_id(lat, lon),
            "observation_month": date(2025, 1, 1),
            "dataset_type": "consolidated_dataset",
            "product_type": "reanalysis",
            "lat": lat,
            "lon": lon,
            "bbox_min_lat": 52.125,
            "bbox_min_lon": 4.625,
            "bbox_max_lat": 52.375,
            "bbox_max_lon": 4.875,
        }
        base.update(overrides)
        return base

    def test_minimal_mapping(self):
        obs = CDSDroughtObservation.from_mapping(self._base())
        assert obs.lat == 52.25
        assert obs.lon == 4.75
        assert obs.observation_month == date(2025, 1, 1)

    def test_observation_month_string_parsed(self):
        obs = CDSDroughtObservation.from_mapping(
            self._base(observation_month="2025-03-01")
        )
        assert obs.observation_month == date(2025, 3, 1)

    def test_spi_fields(self):
        obs = CDSDroughtObservation.from_mapping(
            self._base(spi_1m=-1.5, spi_3m=0.2, spei_6m=1.1)
        )
        assert obs.spi_1m == pytest.approx(-1.5)
        assert obs.spi_3m == pytest.approx(0.2)
        assert obs.spei_6m == pytest.approx(1.1)

    def test_ensemble_member_cast_to_int(self):
        obs = CDSDroughtObservation.from_mapping(self._base(ensemble_member="3"))
        assert obs.ensemble_member == 3

    def test_missing_keys_use_defaults(self):
        obs = CDSDroughtObservation.from_mapping(self._base())
        assert obs.spi_1m is None
        assert obs.ensemble_member == 0
