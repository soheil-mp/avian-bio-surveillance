"""Tests for avian_biosurveillance.data.stations module.

Covers:
- StationProfile / StationStats dataclasses
- extract_station_profiles() with activity metrics
- generate_stations_file() output format and edge cases
- Deduplication, sorting, monthly breakdowns, confidence averaging
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from avian_biosurveillance.data.stations import (
    StationProfile,
    StationStats,
    detect_date_range,
    extract_station_profiles,
    generate_stations_file,
)


# =============================================================================
# Helpers
# =============================================================================


def _make_detection(
    *,
    detection_id: str = "d1",
    timestamp: str = "2026-01-05T12:00:00+01:00",
    station_id: str = "100",
    station_name: str = "Test Station",
    station_type: str = "puc",
    country: str = "Netherlands",
    state: str = "Utrecht",
    lat: float = 52.0,
    lon: float = 5.0,
    species_id: str = "61",
    confidence: float = 0.85,
) -> dict:
    """Build a minimal detection record matching the BirdWeather schema."""
    return {
        "id": detection_id,
        "timestamp": timestamp,
        "confidence": confidence,
        "speciesId": species_id,
        "species": {"id": species_id, "commonName": "Song Thrush"},
        "station": {
            "id": station_id,
            "name": station_name,
            "type": station_type,
            "country": country,
            "state": state,
            "coords": {"lat": lat, "lon": lon},
        },
    }


def _write_detections(directory: Path, records: list[dict]) -> Path:
    """Write a list of detection dicts as a detections.jsonl file."""
    directory.mkdir(parents=True, exist_ok=True)
    jsonl_path = directory / "detections.jsonl"
    jsonl_path.write_text(
        "\n".join(json.dumps(r) for r in records),
        encoding="utf-8",
    )
    return jsonl_path


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture()
def single_station_dir(tmp_path: Path) -> Path:
    """Directory with one day of detections from one station."""
    day = tmp_path / "bw" / "2026-01-05"
    _write_detections(day, [
        _make_detection(detection_id="d1", confidence=0.80),
        _make_detection(detection_id="d2", confidence=0.90),
    ])
    return tmp_path / "bw"


@pytest.fixture()
def multi_station_dir(tmp_path: Path) -> Path:
    """Directory with two stations across two days."""
    day1 = tmp_path / "bw" / "2026-01-01"
    _write_detections(day1, [
        _make_detection(
            detection_id="d1",
            timestamp="2026-01-01T08:00:00+01:00",
            station_id="100",
            station_name="Alpha",
            species_id="61",
            confidence=0.7,
        ),
        _make_detection(
            detection_id="d2",
            timestamp="2026-01-01T09:00:00+01:00",
            station_id="100",
            station_name="Alpha",
            species_id="62",
            confidence=0.9,
        ),
        _make_detection(
            detection_id="d3",
            timestamp="2026-01-01T10:00:00+01:00",
            station_id="200",
            station_name="Beta",
            lat=51.5,
            lon=4.5,
            species_id="61",
            confidence=0.6,
        ),
    ])

    day2 = tmp_path / "bw" / "2026-02-15"
    _write_detections(day2, [
        _make_detection(
            detection_id="d4",
            timestamp="2026-02-15T14:00:00+01:00",
            station_id="100",
            station_name="Alpha",
            species_id="63",
            confidence=0.8,
        ),
    ])
    return tmp_path / "bw"


# =============================================================================
# StationStats Tests
# =============================================================================


class TestStationStats:
    """Tests for the StationStats dataclass."""

    def test_defaults(self) -> None:
        stats = StationStats()
        assert stats.total_detections == 0
        assert stats.unique_species == 0
        assert stats.unique_days == 0
        assert stats.first_detection is None
        assert stats.last_detection is None
        assert stats.avg_confidence is None
        assert stats.monthly_detections == {}

    def test_populated(self) -> None:
        stats = StationStats(
            total_detections=100,
            unique_species=5,
            unique_days=10,
            first_detection="2026-01-01",
            last_detection="2026-01-14",
            avg_confidence=0.75,
            monthly_detections={"2026-01": 100},
        )
        assert stats.total_detections == 100
        assert stats.monthly_detections["2026-01"] == 100


# =============================================================================
# StationProfile Tests
# =============================================================================


class TestStationProfile:
    """Tests for the StationProfile dataclass."""

    def test_to_dict_roundtrip(self) -> None:
        profile = StationProfile(
            id="42",
            name="Test Station",
            type="puc",
            country="NL",
            state="Utrecht",
            coords={"lat": 52.0, "lon": 5.0},
            stats=StationStats(total_detections=10),
        )
        d = profile.to_dict()
        assert d["id"] == "42"
        assert d["name"] == "Test Station"
        assert d["coords"]["lat"] == 52.0
        assert d["stats"]["total_detections"] == 10

    def test_defaults(self) -> None:
        profile = StationProfile(id="1", name="X")
        assert profile.type is None
        assert profile.coords == {}
        assert profile.stats.total_detections == 0


# =============================================================================
# extract_station_profiles Tests
# =============================================================================


class TestExtractStationProfiles:
    """Tests for the core extraction function."""

    def test_single_station(self, single_station_dir: Path) -> None:
        profiles = extract_station_profiles(single_station_dir)
        assert len(profiles) == 1
        p = profiles[0]
        assert p.id == "100"
        assert p.name == "Test Station"
        assert p.type == "puc"
        assert p.country == "Netherlands"
        assert p.state == "Utrecht"
        assert p.coords == {"lat": 52.0, "lon": 5.0}

    def test_detection_count(self, single_station_dir: Path) -> None:
        profiles = extract_station_profiles(single_station_dir)
        assert profiles[0].stats.total_detections == 2

    def test_avg_confidence(self, single_station_dir: Path) -> None:
        profiles = extract_station_profiles(single_station_dir)
        # (0.80 + 0.90) / 2 = 0.85
        assert profiles[0].stats.avg_confidence == pytest.approx(0.85)

    def test_unique_species(self, multi_station_dir: Path) -> None:
        profiles = extract_station_profiles(multi_station_dir)
        alpha = next(p for p in profiles if p.id == "100")
        # Species 61, 62, 63 across two days
        assert alpha.stats.unique_species == 3

    def test_unique_days(self, multi_station_dir: Path) -> None:
        profiles = extract_station_profiles(multi_station_dir)
        alpha = next(p for p in profiles if p.id == "100")
        assert alpha.stats.unique_days == 2

    def test_date_range(self, multi_station_dir: Path) -> None:
        profiles = extract_station_profiles(multi_station_dir)
        alpha = next(p for p in profiles if p.id == "100")
        assert alpha.stats.first_detection == "2026-01-01"
        assert alpha.stats.last_detection == "2026-02-15"

    def test_monthly_detections(self, multi_station_dir: Path) -> None:
        profiles = extract_station_profiles(multi_station_dir)
        alpha = next(p for p in profiles if p.id == "100")
        assert alpha.stats.monthly_detections == {
            "2026-01": 2,
            "2026-02": 1,
        }

    def test_monthly_sorted(self, multi_station_dir: Path) -> None:
        profiles = extract_station_profiles(multi_station_dir)
        alpha = next(p for p in profiles if p.id == "100")
        keys = list(alpha.stats.monthly_detections.keys())
        assert keys == sorted(keys)

    def test_deduplication(self, multi_station_dir: Path) -> None:
        profiles = extract_station_profiles(multi_station_dir)
        assert len(profiles) == 2
        ids = {p.id for p in profiles}
        assert ids == {"100", "200"}

    def test_sorted_by_id(self, multi_station_dir: Path) -> None:
        profiles = extract_station_profiles(multi_station_dir)
        assert [p.id for p in profiles] == sorted(p.id for p in profiles)

    def test_missing_dir_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            extract_station_profiles(tmp_path / "nonexistent")

    def test_empty_dir_raises(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ValueError, match="No stations"):
            extract_station_profiles(empty)

    def test_skips_records_without_station_id(self, tmp_path: Path) -> None:
        day = tmp_path / "bw" / "2026-01-01"
        records = [
            {"id": "d1", "timestamp": "2026-01-01T10:00:00Z", "station": {}},
            _make_detection(detection_id="d2", station_id="valid"),
        ]
        _write_detections(day, records)
        profiles = extract_station_profiles(tmp_path / "bw")
        assert len(profiles) == 1
        assert profiles[0].id == "valid"

    def test_skips_records_without_coords(self, tmp_path: Path) -> None:
        day = tmp_path / "bw" / "2026-01-01"
        records = [
            {
                "id": "d1",
                "timestamp": "2026-01-01T10:00:00Z",
                "station": {"id": "no_coords", "name": "Bad"},
            },
            _make_detection(detection_id="d2", station_id="good"),
        ]
        _write_detections(day, records)
        profiles = extract_station_profiles(tmp_path / "bw")
        assert len(profiles) == 1
        assert profiles[0].id == "good"

    def test_handles_malformed_json_lines(self, tmp_path: Path) -> None:
        day = tmp_path / "bw" / "2026-01-01"
        day.mkdir(parents=True)
        content = (
            "not valid json\n"
            + json.dumps(_make_detection(detection_id="d1"))
            + "\n"
        )
        (day / "detections.jsonl").write_text(content, encoding="utf-8")
        profiles = extract_station_profiles(tmp_path / "bw")
        assert len(profiles) == 1

    def test_confidence_none_ignored(self, tmp_path: Path) -> None:
        day = tmp_path / "bw" / "2026-01-01"
        rec = _make_detection()
        rec["confidence"] = None
        _write_detections(day, [rec])
        profiles = extract_station_profiles(tmp_path / "bw")
        assert profiles[0].stats.avg_confidence is None

    def test_second_station_stats(self, multi_station_dir: Path) -> None:
        profiles = extract_station_profiles(multi_station_dir)
        beta = next(p for p in profiles if p.id == "200")
        assert beta.stats.total_detections == 1
        assert beta.stats.unique_species == 1
        assert beta.stats.unique_days == 1
        assert beta.stats.avg_confidence == pytest.approx(0.6)
        assert beta.stats.monthly_detections == {"2026-01": 1}


# =============================================================================
# generate_stations_file Tests
# =============================================================================


class TestGenerateStationsFile:
    """Tests for the file-writing entry point."""

    def test_default_output_path(self, single_station_dir: Path) -> None:
        result = generate_stations_file(single_station_dir)
        assert result == single_station_dir / "stations.json"
        assert result.exists()

    def test_custom_output_path(
        self, single_station_dir: Path, tmp_path: Path,
    ) -> None:
        custom = tmp_path / "custom" / "out.json"
        result = generate_stations_file(
            single_station_dir, output_path=custom,
        )
        assert result == custom
        assert custom.exists()

    def test_output_contains_stats(self, multi_station_dir: Path) -> None:
        path = generate_stations_file(multi_station_dir)
        data = json.loads(path.read_text())
        assert len(data) == 2

        alpha = next(s for s in data if s["id"] == "100")
        assert "stats" in alpha
        assert alpha["stats"]["total_detections"] == 3
        assert alpha["stats"]["unique_species"] == 3
        assert alpha["stats"]["unique_days"] == 2
        assert alpha["stats"]["first_detection"] == "2026-01-01"
        assert alpha["stats"]["last_detection"] == "2026-02-15"
        assert "2026-01" in alpha["stats"]["monthly_detections"]
        assert "2026-02" in alpha["stats"]["monthly_detections"]

    def test_output_sorted_by_id(self, multi_station_dir: Path) -> None:
        path = generate_stations_file(multi_station_dir)
        data = json.loads(path.read_text())
        ids = [s["id"] for s in data]
        assert ids == sorted(ids)

    def test_raises_on_missing_dir(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            generate_stations_file(tmp_path / "nonexistent")

    def test_raises_on_empty_dir(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(ValueError, match="No stations"):
            generate_stations_file(empty)

    def test_station_metadata_fields(
        self, single_station_dir: Path,
    ) -> None:
        path = generate_stations_file(single_station_dir)
        data = json.loads(path.read_text())
        station = data[0]
        assert station["id"] == "100"
        assert station["name"] == "Test Station"
        assert station["type"] == "puc"
        assert station["country"] == "Netherlands"
        assert station["state"] == "Utrecht"
        assert station["coords"]["lat"] == 52.0
        assert station["coords"]["lon"] == 5.0


# =============================================================================
# detect_date_range Tests
# =============================================================================


class TestDetectDateRange:
    """Tests for the date-range auto-detection function."""

    def test_from_json_path(self, multi_station_dir: Path) -> None:
        """Auto-detect from a generated stations.json file."""
        stations_path = generate_stations_file(multi_station_dir)
        start, end = detect_date_range(stations_path, buffer_days=0)
        # Alpha: 2026-01-01 → 2026-02-15, Beta: 2026-01-01 only
        assert start == "01-01-2026"
        assert end == "02-15-2026"

    def test_from_profiles(self, multi_station_dir: Path) -> None:
        """Auto-detect from StationProfile objects."""
        profiles = extract_station_profiles(multi_station_dir)
        start, end = detect_date_range(profiles, buffer_days=0)
        assert start == "01-01-2026"
        assert end == "02-15-2026"

    def test_from_raw_dicts(self) -> None:
        """Auto-detect from plain dicts (as loaded from JSON)."""
        dicts = [
            {
                "id": "1",
                "stats": {
                    "first_detection": "2025-06-01",
                    "last_detection": "2025-09-30",
                },
            },
            {
                "id": "2",
                "stats": {
                    "first_detection": "2025-07-15",
                    "last_detection": "2025-08-20",
                },
            },
        ]
        start, end = detect_date_range(dicts, buffer_days=0)
        assert start == "06-01-2025"
        assert end == "09-30-2025"

    def test_buffer_applied(self, multi_station_dir: Path) -> None:
        """Buffer pads the range on both sides."""
        stations_path = generate_stations_file(multi_station_dir)
        start, end = detect_date_range(stations_path, buffer_days=10)
        # min=2026-01-01 - 10d = 2025-12-22
        # max=2026-02-15 + 10d = 2026-02-25
        assert start == "12-22-2025"
        assert end == "02-25-2026"

    def test_default_buffer_is_15(self, multi_station_dir: Path) -> None:
        """Default buffer is 15 days."""
        stations_path = generate_stations_file(multi_station_dir)
        start, end = detect_date_range(stations_path)
        # min=2026-01-01 - 15d = 2025-12-17
        # max=2026-02-15 + 15d = 2026-03-02
        assert start == "12-17-2025"
        assert end == "03-02-2026"

    def test_single_station_single_day(self, single_station_dir: Path) -> None:
        """Works with a single station active on one day."""
        stations_path = generate_stations_file(single_station_dir)
        start, end = detect_date_range(stations_path, buffer_days=5)
        # single station: first=last=2026-01-05
        assert start == "12-31-2025"  # 2026-01-05 - 5d
        assert end == "01-10-2026"   # 2026-01-05 + 5d

    def test_empty_list_raises(self) -> None:
        with pytest.raises(ValueError, match="no station data"):
            detect_date_range([])

    def test_no_dates_raises(self) -> None:
        """Stations without detection dates raise ValueError."""
        dicts = [{"id": "1", "stats": {}}]
        with pytest.raises(ValueError, match="No valid detection dates"):
            detect_date_range(dicts)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError, match="not found"):
            detect_date_range(tmp_path / "nonexistent.json")

    def test_partial_dates_uses_available(self) -> None:
        """Stations with partial date info still work."""
        dicts = [
            {
                "id": "1",
                "stats": {
                    "first_detection": "2025-03-01",
                    "last_detection": None,
                },
            },
            {
                "id": "2",
                "stats": {
                    "first_detection": "2025-04-15",
                    "last_detection": "2025-05-20",
                },
            },
        ]
        start, end = detect_date_range(dicts, buffer_days=0)
        assert start == "03-01-2025"
        assert end == "05-20-2025"

    def test_mm_dd_yyyy_format(self) -> None:
        """Output is in AppEEARS MM-DD-YYYY format, not ISO."""
        dicts = [
            {
                "id": "1",
                "stats": {
                    "first_detection": "2025-12-25",
                    "last_detection": "2025-12-25",
                },
            },
        ]
        start, end = detect_date_range(dicts, buffer_days=0)
        assert start == "12-25-2025"  # Not 2025-12-25
        assert end == "12-25-2025"
