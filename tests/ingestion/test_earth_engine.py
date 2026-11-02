from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from avian_biosurveillance.ingestion.earth_engine import (
    CollectStats,
    EarthEngineCollector,
    EarthEngineGridCollector,
    _GridCellSpec,
    _LocationSpec,
)


def _test_grid_cell(cell_id: str, lon_offset: float = 0.0) -> _GridCellSpec:
    min_lon = 5.37 + lon_offset
    max_lon = 5.44 + lon_offset
    return _GridCellSpec(
        grid_cell_id=cell_id,
        grid_system="nl_geodesic_5km",
        source="netherlands_geodesic_grid",
        lat=52.10,
        lon=(min_lon + max_lon) / 2,
        bbox_min_lat=52.08,
        bbox_min_lon=min_lon,
        bbox_max_lat=52.12,
        bbox_max_lon=max_lon,
        grid_size_km=5.0,
        cell_geojson={
            "type": "Polygon",
            "coordinates": [
                [
                    [min_lon, 52.08],
                    [max_lon, 52.08],
                    [max_lon, 52.12],
                    [min_lon, 52.12],
                    [min_lon, 52.08],
                ]
            ],
        },
    )


def test_profile_station_adds_meta_for_batch_collection(tmp_path: Path) -> None:
    def fake_run_section(name, lat, lon, date, geometry=None):
        if name == "elevation":
            return {"elevation_m": 12.5, "slope_degrees": 1.1, "aspect_degrees": 90.0}
        if name == "climate":
            return {"temperature_2m_c": 18.4}
        raise AssertionError(f"unexpected section {name}")

    with (
        patch(
            "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
            return_value=MagicMock(),
        ),
        patch(
            "avian_biosurveillance.ingestion.earth_engine.run_section",
            side_effect=fake_run_section,
        ),
    ):
        collector = EarthEngineCollector(
            output_dir=tmp_path,
            parallel=False,
            sections=["elevation", "climate"],
        )

        profile = collector._profile_station(
            _LocationSpec(
                station_id="BW-001",
                source="birdweather_api",
                lat=52.1,
                lon=5.4,
            ),
            "2026-03-23",
        )

    assert profile["_meta"]["mode"] == "sequential"
    assert profile["_meta"]["total_seconds"] >= 0
    assert profile["section_availability"]["elevation"]["status"] == "available"
    assert profile["section_availability"]["climate"]["status"] == "available"
    assert profile["section_availability"]["vegetation"]["status"] == "not_queried"


def test_collect_for_date_rejects_partial_database_profiles(tmp_path: Path) -> None:
    with patch(
        "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
        return_value=MagicMock(),
    ):
        collector = EarthEngineCollector(
            output_dir=tmp_path,
            sections=["climate"],
        )

    with pytest.raises(ValueError, match="full Earth Engine profile"):
        collector.collect_for_date(
            date(2026, 3, 23),
            [
                _LocationSpec(
                    station_id="BW-001",
                    source="birdweather_api",
                    lat=52.1,
                    lon=5.4,
                )
            ],
            target="database",
        )


def test_discover_grid_cells_builds_geodesic_cells() -> None:
    cells = EarthEngineGridCollector.discover_grid_cells(
        grid_size_km=5.0,
        sw={"lat": 50.70, "lon": 3.30},
        ne={"lat": 50.76, "lon": 3.36},
    )

    assert cells
    first = cells[0]
    assert first.grid_cell_id.startswith("nl-5km-r000-c")
    assert first.grid_system == "nl_geodesic_5km"
    assert first.grid_size_km == 5.0
    assert first.bbox_max_lat > first.bbox_min_lat
    assert first.bbox_max_lon > first.bbox_min_lon
    assert first.bbox_min_lat <= first.lat <= first.bbox_max_lat
    assert first.bbox_min_lon <= first.lon <= first.bbox_max_lon
    assert first.cell_geojson["type"] == "Polygon"


def test_discover_grid_cells_reuses_shared_vertices_between_adjacent_cells() -> None:
    cells = EarthEngineGridCollector.discover_grid_cells(
        grid_size_km=5.0,
        sw={"lat": 50.70, "lon": 3.30},
        ne={"lat": 50.82, "lon": 3.46},
    )

    by_id = {cell.grid_cell_id: cell for cell in cells}
    assert "nl-5km-r000-c000" in by_id
    assert "nl-5km-r000-c001" in by_id
    assert "nl-5km-r001-c000" in by_id

    row0_col0 = by_id["nl-5km-r000-c000"].cell_geojson["coordinates"][0]
    row0_col1 = by_id["nl-5km-r000-c001"].cell_geojson["coordinates"][0]
    row1_col0 = by_id["nl-5km-r001-c000"].cell_geojson["coordinates"][0]

    # Horizontal neighbors share the exact east/west boundary.
    assert row0_col0[1] == row0_col1[0]
    assert row0_col0[2] == row0_col1[3]

    # Vertical neighbors share the exact north/south boundary.
    assert row0_col0[3] == row1_col0[0]
    assert row0_col0[2] == row1_col0[1]


def test_discover_grid_cells_clips_to_country_geometry() -> None:
    cells = EarthEngineGridCollector.discover_grid_cells(
        grid_size_km=5.0,
        sw={"lat": 50.70, "lon": 3.30},
        ne={"lat": 50.82, "lon": 3.46},
    )
    assert len(cells) >= 2

    clipped = EarthEngineGridCollector.discover_grid_cells(
        grid_size_km=5.0,
        sw={"lat": 50.70, "lon": 3.30},
        ne={"lat": 50.82, "lon": 3.46},
        country_geometry=cells[0].cell_geojson,
    )

    assert [cell.grid_cell_id for cell in clipped] == [cells[0].grid_cell_id]


def test_grid_collect_for_date_saves_grid_metadata(tmp_path: Path) -> None:
    profiler = MagicMock()
    profiler.get_elevation.return_value = {"elevation_m": 12.5}

    with patch(
        "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
        return_value=profiler,
    ):
        collector = EarthEngineGridCollector(
            output_dir=tmp_path,
            parallel=False,
            sections=["elevation"],
            grid_size_km=5.0,
        )

    stats = collector.collect_for_date(
        date(2026, 3, 23),
        [
            _GridCellSpec(
                grid_cell_id="nl-5km-r000-c000",
                grid_system="nl_geodesic_5km",
                source="netherlands_geodesic_grid",
                lat=52.10,
                lon=5.40,
                bbox_min_lat=52.08,
                bbox_min_lon=5.37,
                bbox_max_lat=52.12,
                bbox_max_lon=5.44,
                grid_size_km=5.0,
                cell_geojson={
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [5.37, 52.08],
                            [5.44, 52.08],
                            [5.44, 52.12],
                            [5.37, 52.12],
                            [5.37, 52.08],
                        ]
                    ],
                },
            )
        ],
        target="local",
    )

    metadata = json.loads(
        (tmp_path / "2026-03-23" / "metadata.json").read_text(encoding="utf-8")
    )
    profiles = json.loads(
        (tmp_path / "2026-03-23" / "profiles.json").read_text(encoding="utf-8")
    )

    assert stats.locations_profiled == 1
    assert metadata["collection_type"] == "grid"
    assert metadata["results"]["grid_cells_queried"] == 1
    assert profiles[0]["grid_cell_id"] == "nl-5km-r000-c000"
    assert profiles[0]["grid_size_km"] == 5.0


def test_grid_collect_for_date_parallel_cells_preserves_input_order(
    tmp_path: Path,
) -> None:
    with patch(
        "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
        return_value=MagicMock(),
    ):
        collector = EarthEngineGridCollector(
            output_dir=tmp_path,
            parallel=False,
            sections=["elevation"],
            grid_size_km=5.0,
            cell_workers=2,
        )

    cells = [
        _GridCellSpec(
            grid_cell_id="nl-5km-r000-c000",
            grid_system="nl_geodesic_5km",
            source="netherlands_geodesic_grid",
            lat=52.10,
            lon=5.40,
            bbox_min_lat=52.08,
            bbox_min_lon=5.37,
            bbox_max_lat=52.12,
            bbox_max_lon=5.44,
            grid_size_km=5.0,
            cell_geojson={
                "type": "Polygon",
                "coordinates": [
                    [
                        [5.37, 52.08],
                        [5.44, 52.08],
                        [5.44, 52.12],
                        [5.37, 52.12],
                        [5.37, 52.08],
                    ]
                ],
            },
        ),
        _GridCellSpec(
            grid_cell_id="nl-5km-r000-c001",
            grid_system="nl_geodesic_5km",
            source="netherlands_geodesic_grid",
            lat=52.10,
            lon=5.47,
            bbox_min_lat=52.08,
            bbox_min_lon=5.44,
            bbox_max_lat=52.12,
            bbox_max_lon=5.51,
            grid_size_km=5.0,
            cell_geojson={
                "type": "Polygon",
                "coordinates": [
                    [
                        [5.44, 52.08],
                        [5.51, 52.08],
                        [5.51, 52.12],
                        [5.44, 52.12],
                        [5.44, 52.08],
                    ]
                ],
            },
        ),
    ]

    def _profile(cell: _GridCellSpec, _date_str: str) -> dict[str, object]:
        if cell.grid_cell_id.endswith("c000"):
            time.sleep(0.02)
        return {
            "grid_cell_id": cell.grid_cell_id,
            "source": cell.source,
            "lat": cell.lat,
            "lon": cell.lon,
            "date": "2026-03-23",
            "grid_size_km": cell.grid_size_km,
        }

    with patch.object(collector, "_profile_grid_cell", side_effect=_profile):
        stats = collector.collect_for_date(
            date(2026, 3, 23),
            cells,
            target="local",
        )

    profiles = json.loads(
        (tmp_path / "2026-03-23" / "profiles.json").read_text(encoding="utf-8")
    )

    assert stats.locations_profiled == 2
    assert [profile["grid_cell_id"] for profile in profiles] == [
        "nl-5km-r000-c000",
        "nl-5km-r000-c001",
    ]


def test_grid_collector_caps_nested_workers_to_http_pool(tmp_path: Path) -> None:
    with patch(
        "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
        return_value=MagicMock(),
    ):
        collector = EarthEngineGridCollector(
            output_dir=tmp_path,
            parallel=True,
            sections=["elevation"],
            grid_size_km=5.0,
            max_workers=6,
            cell_workers=4,
        )

    assert collector.cell_workers == 4
    assert collector.max_workers == 2


@patch(
    "avian_biosurveillance.ingestion.earth_engine."
    "EarthEngineGridCollector._ensure_grid_cells_registered"
)
@patch(
    "avian_biosurveillance.ingestion.earth_engine."
    "EarthEngineGridCollector._flush_grid_profiles_to_db"
)
def test_grid_collect_for_date_registers_canonical_grid_cells_before_db_flush(
    mock_flush: MagicMock,
    mock_register: MagicMock,
    tmp_path: Path,
) -> None:
    profiler = MagicMock()
    profiler.get_elevation.return_value = {"elevation_m": 12.5}

    with patch(
        "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
        return_value=profiler,
    ):
        collector_sections = list(EarthEngineCollector._SECTIONS)
        collector = EarthEngineGridCollector(
            output_dir=tmp_path,
            parallel=False,
            sections=collector_sections,
            grid_size_km=5.0,
        )

    collector.collect_for_date(
        date(2026, 3, 23),
        [
            _GridCellSpec(
                grid_cell_id="nl-5km-r000-c000",
                grid_system="nl_geodesic_5km",
                source="netherlands_geodesic_grid",
                lat=52.10,
                lon=5.40,
                bbox_min_lat=52.08,
                bbox_min_lon=5.37,
                bbox_max_lat=52.12,
                bbox_max_lon=5.44,
                grid_size_km=5.0,
                cell_geojson={
                    "type": "Polygon",
                    "coordinates": [
                        [
                            [5.37, 52.08],
                            [5.44, 52.08],
                            [5.44, 52.12],
                            [5.37, 52.12],
                            [5.37, 52.08],
                        ]
                    ],
                },
            )
        ],
        target="database",
    )

    assert collector.sections == collector_sections
    mock_register.assert_called_once()
    mock_flush.assert_called_once()


def test_static_sections_are_cached_across_date_range(tmp_path: Path) -> None:
    calls: list[str | None] = []

    def fake_run_section(name, lat, lon, date_str, geometry=None):
        assert name == "elevation"
        calls.append(date_str)
        return {"elevation_m": 12.5, "slope_degrees": 1.1, "aspect_degrees": 90.0}

    with (
        patch(
            "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
            return_value=MagicMock(),
        ),
        patch(
            "avian_biosurveillance.ingestion.earth_engine.run_section",
            side_effect=fake_run_section,
        ),
    ):
        collector = EarthEngineCollector(
            output_dir=tmp_path,
            parallel=False,
            sections=["elevation"],
        )

        collector.collect_date_range(
            date(2026, 3, 23),
            date(2026, 3, 24),
            [
                _LocationSpec(
                    station_id="BW-001",
                    source="birdweather_api",
                    lat=52.1,
                    lon=5.4,
                )
            ],
            target="local",
        )

    assert calls == [None]


@patch(
    "avian_biosurveillance.ingestion.earth_engine."
    "EarthEngineGridCollector._ensure_grid_cells_registered"
)
@patch(
    "avian_biosurveillance.ingestion.earth_engine."
    "EarthEngineGridCollector._flush_grid_profiles_to_db"
)
def test_grid_collect_for_date_skips_complete_database_date(
    mock_flush: MagicMock,
    mock_register: MagicMock,
    tmp_path: Path,
) -> None:
    pool = MagicMock()
    cur = pool.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [
        ("nl-5km-r000-c000",),
        ("nl-5km-r000-c001",),
    ]

    with (
        patch(
            "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
            return_value=MagicMock(),
        ),
        patch(
            "avian_biosurveillance.db.database.get_pool",
            return_value=pool,
        ),
    ):
        collector = EarthEngineGridCollector(
            output_dir=tmp_path,
            parallel=False,
            sections=list(EarthEngineCollector._SECTIONS),
            grid_size_km=5.0,
        )
        cells = [
            _test_grid_cell("nl-5km-r000-c000"),
            _test_grid_cell("nl-5km-r000-c001", lon_offset=0.07),
        ]

        with patch.object(
            collector,
            "_profile_grid_cell",
            side_effect=AssertionError("existing date should not be profiled"),
        ):
            stats = collector.collect_for_date(
                date(2026, 3, 23),
                cells,
                target="database",
            )

    assert stats.locations_profiled == 0
    assert stats.locations_skipped == 2
    assert stats.locations_failed == 0
    mock_register.assert_not_called()
    mock_flush.assert_not_called()


@patch(
    "avian_biosurveillance.ingestion.earth_engine."
    "EarthEngineGridCollector._ensure_grid_cells_registered"
)
@patch(
    "avian_biosurveillance.ingestion.earth_engine."
    "EarthEngineGridCollector._flush_grid_profiles_to_db"
)
def test_grid_collect_for_date_resumes_missing_database_cells(
    mock_flush: MagicMock,
    mock_register: MagicMock,
    tmp_path: Path,
) -> None:
    pool = MagicMock()
    cur = pool.cursor.return_value.__enter__.return_value
    cur.fetchall.return_value = [("nl-5km-r000-c000",)]

    with (
        patch(
            "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
            return_value=MagicMock(),
        ),
        patch(
            "avian_biosurveillance.db.database.get_pool",
            return_value=pool,
        ),
    ):
        collector = EarthEngineGridCollector(
            output_dir=tmp_path,
            parallel=False,
            sections=list(EarthEngineCollector._SECTIONS),
            grid_size_km=5.0,
        )
        cells = [
            _test_grid_cell("nl-5km-r000-c000"),
            _test_grid_cell("nl-5km-r000-c001", lon_offset=0.07),
        ]

        def _profile(cell: _GridCellSpec, date_str: str) -> dict[str, object]:
            return {
                "grid_cell_id": cell.grid_cell_id,
                "source": cell.source,
                "lat": cell.lat,
                "lon": cell.lon,
                "date": date_str,
                "grid_size_km": cell.grid_size_km,
            }

        with patch.object(
            collector,
            "_profile_grid_cell",
            side_effect=_profile,
        ) as mock_profile:
            stats = collector.collect_for_date(
                date(2026, 3, 23),
                cells,
                target="database",
            )

    assert stats.locations_profiled == 1
    assert stats.locations_skipped == 1
    assert stats.locations_failed == 0
    assert mock_profile.call_args.args[0].grid_cell_id == "nl-5km-r000-c001"
    mock_register.assert_called_once()
    mock_flush.assert_called_once()


def test_grid_collect_date_range_skips_complete_database_days(
    tmp_path: Path,
) -> None:
    with patch(
        "avian_biosurveillance.ingestion.earth_engine.EarthEngineProfiler",
        return_value=MagicMock(),
    ):
        collector = EarthEngineGridCollector(
            output_dir=tmp_path,
            parallel=False,
            sections=list(EarthEngineCollector._SECTIONS),
            grid_size_km=5.0,
        )

    cells = [
        _test_grid_cell("nl-5km-r000-c000"),
        _test_grid_cell("nl-5km-r000-c001", lon_offset=0.07),
    ]

    with (
        patch.object(
            collector,
            "_existing_grid_profile_counts_by_date",
            return_value={date(2026, 3, 23): 2},
        ),
        patch.object(
            collector,
            "collect_for_date",
            return_value=CollectStats(date="2026-03-24", locations_profiled=2),
        ) as mock_collect,
    ):
        stats = collector.collect_date_range(
            date(2026, 3, 23),
            date(2026, 3, 24),
            cells,
            target="database",
        )

    assert [item.date for item in stats] == ["2026-03-23", "2026-03-24"]
    assert stats[0].locations_skipped == 2
    mock_collect.assert_called_once_with(
        date(2026, 3, 24),
        cells,
        target="database",
        force=False,
    )
