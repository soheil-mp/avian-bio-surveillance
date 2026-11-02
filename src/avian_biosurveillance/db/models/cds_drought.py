"""CDS drought grid data model.

Maps to ``cds_drought_tbl``. Each row stores a wide monthly drought profile for
one 0.25° Copernicus CDS grid cell, dataset variant, product type, and ensemble
member.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from datetime import date
from typing import Any


DEFAULT_CDS_GRID_SYSTEM = "cds_latlon_0p25deg"
DEFAULT_CDS_SOURCE = "copernicus_cds_era5_drought"
DEFAULT_CDS_RESOLUTION_DEGREES = 0.25


def build_cds_grid_cell_id(
    lat: float,
    lon: float,
    *,
    resolution_degrees: float = DEFAULT_CDS_RESOLUTION_DEGREES,
) -> str:
    """Return a stable textual identifier for a CDS 0.25° grid point."""
    return f"cds_{resolution_degrees:g}deg_lat_{lat:.2f}_lon_{lon:.2f}"


def build_cds_bbox(
    lat: float,
    lon: float,
    *,
    resolution_degrees: float = DEFAULT_CDS_RESOLUTION_DEGREES,
) -> tuple[float, float, float, float]:
    """Return the bounding box for a CDS grid-cell centroid."""
    half = resolution_degrees / 2
    return (
        round(lat - half, 6),
        round(lon - half, 6),
        round(lat + half, 6),
        round(lon + half, 6),
    )


@dataclass(frozen=True, slots=True)
class CDSDroughtObservation:
    """One monthly CDS drought row for a single grid/product/member tuple."""

    grid_cell_id: str
    observation_month: date
    dataset_type: str
    product_type: str
    lat: float
    lon: float
    bbox_min_lat: float
    bbox_min_lon: float
    bbox_max_lat: float
    bbox_max_lon: float

    ensemble_member: int = 0
    data_version: str = "1_0"
    grid_system: str = DEFAULT_CDS_GRID_SYSTEM
    source: str = DEFAULT_CDS_SOURCE
    resolution_degrees: float = DEFAULT_CDS_RESOLUTION_DEGREES

    spi_1m: float | None = None
    spi_3m: float | None = None
    spi_6m: float | None = None
    spi_12m: float | None = None
    spi_24m: float | None = None
    spi_36m: float | None = None
    spi_48m: float | None = None

    spei_1m: float | None = None
    spei_3m: float | None = None
    spei_6m: float | None = None
    spei_12m: float | None = None
    spei_24m: float | None = None
    spei_36m: float | None = None
    spei_48m: float | None = None

    spi_p0_1m: float | None = None
    spi_p0_3m: float | None = None
    spi_p0_6m: float | None = None
    spi_p0_12m: float | None = None
    spi_p0_24m: float | None = None
    spi_p0_36m: float | None = None
    spi_p0_48m: float | None = None

    spi_normality_1m: bool | None = None
    spi_normality_3m: bool | None = None
    spi_normality_6m: bool | None = None
    spi_normality_12m: bool | None = None
    spi_normality_24m: bool | None = None
    spi_normality_36m: bool | None = None
    spi_normality_48m: bool | None = None

    spei_normality_1m: bool | None = None
    spei_normality_3m: bool | None = None
    spei_normality_6m: bool | None = None
    spei_normality_12m: bool | None = None
    spei_normality_24m: bool | None = None
    spei_normality_36m: bool | None = None
    spei_normality_48m: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a flat dict suitable for DataFrames or JSON serialisation."""
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @staticmethod
    def column_names() -> list[str]:
        """Return the ordered DB column names for this model."""
        return [field.name for field in fields(CDSDroughtObservation)]

    @classmethod
    def from_mapping(
        cls,
        mapping: Mapping[str, Any],
    ) -> CDSDroughtObservation:
        """Build a dataclass instance from a flat mapping."""
        values: dict[str, Any] = {}
        for field in fields(cls):
            if field.name not in mapping:
                continue
            value = mapping[field.name]
            if field.name == "observation_month" and isinstance(value, str):
                value = date.fromisoformat(value)
            if field.name == "ensemble_member" and value is not None:
                value = int(value)
            values[field.name] = value
        return cls(**values)
