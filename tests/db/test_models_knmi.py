"""Tests for avian_biosurveillance.db.models.knmi."""

from __future__ import annotations

import json
from dataclasses import fields
from datetime import UTC, date, datetime

import pytest

from avian_biosurveillance.db.models.knmi import KNMIWeatherObservation, SeismicEvent


# ---------------------------------------------------------------------------
# KNMIWeatherObservation
# ---------------------------------------------------------------------------


def _make_coverage(timestamps: list[str], **ranges) -> dict:
    """Build a minimal CoverageJSON coverage dict."""
    base_ranges = {
        "ta": [20.0] * len(timestamps),
        "rh": [75.0] * len(timestamps),
        "ff": [3.0] * len(timestamps),
        "dd": [180.0] * len(timestamps),
        "pp": [1013.0] * len(timestamps),
        "R1H": [0.0] * len(timestamps),
        "qg": [150.0] * len(timestamps),
        "n": [4.0] * len(timestamps),
    }
    base_ranges.update(ranges)
    return {
        "domain": {
            "axes": {
                "t": {"values": timestamps},
                "x": {"values": [5.18]},
                "y": {"values": [52.10]},
            }
        },
        "ranges": {key: {"values": vals} for key, vals in base_ranges.items()},
    }


class TestKNMIWeatherObservationFromEdrCoverage:
    def test_single_timestamp(self):
        cov = _make_coverage(["2026-01-15T10:00:00Z"])
        observations = KNMIWeatherObservation.from_edr_coverage(
            cov, location_id="350", location_name="De Bilt"
        )
        assert len(observations) == 1
        obs = observations[0]
        assert obs.station_id == "350"
        assert obs.station_name == "De Bilt"
        assert obs.observed_at == datetime(2026, 1, 15, 10, 0, 0, tzinfo=UTC)
        assert obs.observation_date == date(2026, 1, 15)
        assert obs.temperature_c == 20.0
        assert obs.relative_humidity_pct == 75.0
        assert obs.wind_speed_ms == 3.0
        assert obs.pressure_hpa == 1013.0

    def test_multiple_timestamps(self):
        ts = ["2026-01-15T10:00:00Z", "2026-01-15T10:10:00Z", "2026-01-15T10:20:00Z"]
        cov = _make_coverage(ts)
        observations = KNMIWeatherObservation.from_edr_coverage(cov, "350")
        assert len(observations) == 3

    def test_station_lat_lon_from_coverage(self):
        cov = _make_coverage(["2026-01-15T10:00:00Z"])
        obs = KNMIWeatherObservation.from_edr_coverage(
            cov, "350", location_elevation_m=2.0
        )[0]
        assert obs.station_lat == 52.10
        assert obs.station_lon == 5.18
        assert obs.station_elevation_m == 2.0

    def test_missing_range_defaults_none(self):
        cov = {
            "domain": {
                "axes": {
                    "t": {"values": ["2026-01-15T10:00:00Z"]},
                    "x": {"values": [5.18]},
                    "y": {"values": [52.10]},
                }
            },
            "ranges": {},
        }
        obs = KNMIWeatherObservation.from_edr_coverage(cov, "350")[0]
        assert obs.temperature_c is None
        assert obs.wind_speed_ms is None

    def test_empty_timestamps_returns_empty_list(self):
        cov = _make_coverage([])
        result = KNMIWeatherObservation.from_edr_coverage(cov, "350")
        assert result == []


class TestKNMIWeatherObservationHelpers:
    def _make(self) -> KNMIWeatherObservation:
        return KNMIWeatherObservation(
            observed_at=datetime(2026, 1, 15, 10, 0, tzinfo=UTC),
            observation_date=date(2026, 1, 15),
            station_id="350",
            station_name="De Bilt",
            temperature_c=15.0,
        )

    def test_to_dict_has_all_fields(self):
        obs = self._make()
        d = obs.to_dict()
        assert set(d.keys()) == {f.name for f in fields(obs)}

    def test_column_names_matches_to_dict(self):
        obs = self._make()
        assert set(KNMIWeatherObservation.column_names()) == set(obs.to_dict().keys())

    def test_pandas_dtypes_keys_match_column_names(self):
        assert set(KNMIWeatherObservation.pandas_dtypes().keys()) == set(
            KNMIWeatherObservation.column_names()
        )

    def test_frozen(self):
        obs = self._make()
        with pytest.raises((AttributeError, TypeError)):
            obs.station_id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# SeismicEvent
# ---------------------------------------------------------------------------


def _minimal_seismic(**overrides) -> dict:
    base = {
        "event_id": "quake-001",
        "time": "2026-01-10T03:15:00+00:00",
        "latitude": 53.2,
        "longitude": 6.8,
        "depth": 3.0,
        "magnitude": 2.5,
        "magnitude_type": "ML",
        "description": "Groningen earthquake",
        "region": "Groningen",
    }
    base.update(overrides)
    return base


class TestSeismicEventFromApi:
    def test_basic_fields(self):
        ev = SeismicEvent.from_api(_minimal_seismic())
        assert ev.event_id == "quake-001"
        assert ev.latitude == 53.2
        assert ev.longitude == 6.8
        assert ev.depth_km == 3.0
        assert ev.magnitude == 2.5
        assert ev.magnitude_type == "ML"
        assert ev.region == "Groningen"

    def test_event_time_from_iso_string(self):
        ev = SeismicEvent.from_api(_minimal_seismic(time="2026-01-10T03:15:00+00:00"))
        assert ev.event_time == datetime(2026, 1, 10, 3, 15, 0, tzinfo=UTC)

    def test_event_time_from_datetime_object(self):
        ts = datetime(2026, 1, 10, 3, 15, 0, tzinfo=UTC)
        ev = SeismicEvent.from_api(_minimal_seismic(time=ts))
        assert ev.event_time == ts

    def test_extra_fields_in_attributes(self):
        raw = _minimal_seismic(custom_field="extra_value", another=123)
        ev = SeismicEvent.from_api(raw)
        attrs = json.loads(ev.attributes)
        assert attrs.get("custom_field") == "extra_value"
        assert attrs.get("another") == 123

    def test_no_extra_fields_empty_attributes(self):
        ev = SeismicEvent.from_api(_minimal_seismic())
        assert ev.attributes == "{}"

    def test_optional_fields_none(self):
        ev = SeismicEvent.from_api(
            {
                "event_id": "x",
                "time": "2026-01-10T00:00:00+00:00",
                "latitude": 52.0,
                "longitude": 5.0,
            }
        )
        assert ev.depth_km is None
        assert ev.magnitude is None
        assert ev.description is None

    def test_to_dict_has_all_fields(self):
        ev = SeismicEvent.from_api(_minimal_seismic())
        d = ev.to_dict()
        assert set(d.keys()) == {f.name for f in fields(ev)}
