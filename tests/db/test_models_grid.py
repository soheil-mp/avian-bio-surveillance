"""Tests for avian_biosurveillance.db.models.grid."""

from __future__ import annotations

import json
from dataclasses import fields

import pytest

from avian_biosurveillance.db.models.grid import GridCell


def _make_grid_cell(**overrides) -> GridCell:
    defaults = dict(
        grid_cell_id="nl_5km_52.0_5.0",
        grid_system="nl_5km",
        source="custom",
        lat=52.0,
        lon=5.0,
        bbox_min_lat=51.975,
        bbox_min_lon=4.975,
        bbox_max_lat=52.025,
        bbox_max_lon=5.025,
        grid_size_km=5.0,
        cell_geojson='{"type": "Point", "coordinates": [5.0, 52.0]}',
    )
    defaults.update(overrides)
    return GridCell(**defaults)


class TestGridCell:
    def test_instantiation(self):
        cell = _make_grid_cell()
        assert cell.grid_cell_id == "nl_5km_52.0_5.0"
        assert cell.lat == 52.0
        assert cell.lon == 5.0
        assert cell.grid_size_km == 5.0

    def test_optional_fields_default_none(self):
        cell = _make_grid_cell()
        assert cell.grid_row is None
        assert cell.grid_col is None

    def test_frozen(self):
        cell = _make_grid_cell()
        with pytest.raises((AttributeError, TypeError)):
            cell.lat = 0.0  # type: ignore[misc]


class TestFromSpec:
    def test_basic(self):
        geojson = {"type": "Point", "coordinates": [5.0, 52.0]}
        cell = GridCell.from_spec(
            grid_cell_id="test",
            grid_system="test_sys",
            source=None,
            lat=52.0,
            lon=5.0,
            bbox_min_lat=51.95,
            bbox_min_lon=4.95,
            bbox_max_lat=52.05,
            bbox_max_lon=5.05,
            grid_size_km=10.0,
            cell_geojson=geojson,
        )
        assert cell.grid_cell_id == "test"
        assert cell.source is None
        assert json.loads(cell.cell_geojson) == geojson

    def test_with_row_col(self):
        cell = GridCell.from_spec(
            grid_cell_id="r0c0",
            grid_system="grid",
            source="test",
            lat=52.0,
            lon=5.0,
            bbox_min_lat=51.9,
            bbox_min_lon=4.9,
            bbox_max_lat=52.1,
            bbox_max_lon=5.1,
            grid_size_km=10.0,
            cell_geojson={"type": "Polygon"},
            grid_row=0,
            grid_col=0,
        )
        assert cell.grid_row == 0
        assert cell.grid_col == 0

    def test_geojson_serialized_as_string(self):
        geojson = {"type": "Feature", "properties": {"name": "test"}}
        cell = GridCell.from_spec(
            grid_cell_id="x",
            grid_system="s",
            source=None,
            lat=52.0,
            lon=5.0,
            bbox_min_lat=51.9,
            bbox_min_lon=4.9,
            bbox_max_lat=52.1,
            bbox_max_lon=5.1,
            grid_size_km=1.0,
            cell_geojson=geojson,
        )
        assert isinstance(cell.cell_geojson, str)


class TestHelpers:
    def test_to_dict_has_all_fields(self):
        cell = _make_grid_cell()
        d = cell.to_dict()
        assert set(d.keys()) == {f.name for f in fields(cell)}

    def test_column_names_matches_to_dict(self):
        cell = _make_grid_cell()
        assert set(GridCell.column_names()) == set(cell.to_dict().keys())
