"""Canonical grid-cell models shared across data sources."""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from typing import Any


@dataclass(frozen=True, slots=True)
class GridCell:
    """One canonical grid cell in the shared Netherlands tessellation."""

    grid_cell_id: str
    grid_system: str
    source: str | None
    lat: float
    lon: float
    bbox_min_lat: float
    bbox_min_lon: float
    bbox_max_lat: float
    bbox_max_lon: float
    grid_size_km: float
    cell_geojson: str
    grid_row: int | None = None
    grid_col: int | None = None

    @classmethod
    def from_spec(
        cls,
        *,
        grid_cell_id: str,
        grid_system: str,
        source: str | None,
        lat: float,
        lon: float,
        bbox_min_lat: float,
        bbox_min_lon: float,
        bbox_max_lat: float,
        bbox_max_lon: float,
        grid_size_km: float,
        cell_geojson: dict[str, Any],
        grid_row: int | None = None,
        grid_col: int | None = None,
    ) -> "GridCell":
        return cls(
            grid_cell_id=grid_cell_id,
            grid_system=grid_system,
            source=source,
            lat=lat,
            lon=lon,
            bbox_min_lat=bbox_min_lat,
            bbox_min_lon=bbox_min_lon,
            bbox_max_lat=bbox_max_lat,
            bbox_max_lon=bbox_max_lon,
            grid_size_km=grid_size_km,
            cell_geojson=json.dumps(cell_geojson, default=str),
            grid_row=grid_row,
            grid_col=grid_col,
        )

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @staticmethod
    def column_names() -> list[str]:
        return [f.name for f in fields(GridCell)]
