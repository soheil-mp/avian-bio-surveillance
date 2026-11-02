"""Tests for avian_biosurveillance.db.models.movebank."""

from __future__ import annotations

from dataclasses import fields
from datetime import datetime

import pytest

from avian_biosurveillance.db.models.movebank import (
    MovebankEvent,
    _clamp_lat,
    _clamp_lon,
    _parse_bool,
    _parse_float,
    _parse_int,
    _parse_timestamp,
)


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestParseFloat:
    def test_valid_string(self):
        assert _parse_float("3.14") == pytest.approx(3.14)

    def test_valid_int(self):
        assert _parse_float(5) == 5.0

    def test_empty_string(self):
        assert _parse_float("") is None

    def test_none(self):
        assert _parse_float(None) is None

    def test_invalid_string(self):
        assert _parse_float("abc") is None


class TestParseInt:
    def test_valid_string(self):
        assert _parse_int("42") == 42

    def test_float_string(self):
        assert _parse_int("3.9") == 3

    def test_empty_string(self):
        assert _parse_int("") is None

    def test_none(self):
        assert _parse_int(None) is None

    def test_invalid(self):
        assert _parse_int("abc") is None


class TestParseBool:
    def test_true_string(self):
        assert _parse_bool("true") is True
        assert _parse_bool("True") is True
        assert _parse_bool("TRUE") is True

    def test_false_string(self):
        assert _parse_bool("false") is False

    def test_bool_passthrough(self):
        assert _parse_bool(True) is True
        assert _parse_bool(False) is False

    def test_empty_string(self):
        assert _parse_bool("") is None

    def test_none(self):
        assert _parse_bool(None) is None


class TestParseTimestamp:
    def test_full_format(self):
        result = _parse_timestamp("2026-01-15 10:30:00.000")
        assert result == datetime(2026, 1, 15, 10, 30, 0)

    def test_no_microseconds(self):
        result = _parse_timestamp("2026-01-15 10:30:00")
        assert result == datetime(2026, 1, 15, 10, 30, 0)

    def test_date_only(self):
        result = _parse_timestamp("2026-01-15")
        assert result == datetime(2026, 1, 15, 0, 0, 0)

    def test_passthrough_datetime(self):
        dt = datetime(2026, 1, 15)
        assert _parse_timestamp(dt) is dt

    def test_empty_string(self):
        assert _parse_timestamp("") is None

    def test_none(self):
        assert _parse_timestamp(None) is None

    def test_invalid(self):
        assert _parse_timestamp("not a date") is None


class TestClampLat:
    def test_valid_range(self):
        assert _clamp_lat(52.0) == 52.0
        assert _clamp_lat(-90.0) == -90.0
        assert _clamp_lat(90.0) == 90.0

    def test_out_of_range(self):
        assert _clamp_lat(91.0) is None
        assert _clamp_lat(-91.0) is None

    def test_none(self):
        assert _clamp_lat(None) is None


class TestClampLon:
    def test_valid_range(self):
        assert _clamp_lon(5.0) == 5.0
        assert _clamp_lon(-180.0) == -180.0
        assert _clamp_lon(180.0) == 180.0

    def test_out_of_range(self):
        assert _clamp_lon(181.0) is None
        assert _clamp_lon(-181.0) is None

    def test_none(self):
        assert _clamp_lon(None) is None


# ---------------------------------------------------------------------------
# MovebankEvent.from_csv_row
# ---------------------------------------------------------------------------


def _minimal_row(**overrides) -> dict:
    base = {
        "event_id": "1001",
        "study_id": "999",
        "timestamp": "2026-01-15 08:00:00",
        "location_lat": "52.1",
        "location_long": "5.0",
        "individual_id": "42",
        "individual_local_identifier": "Blackbird-A",
    }
    base.update(overrides)
    return base


class TestMovebankEventFromCsvRow:
    def test_minimal_row(self):
        ev = MovebankEvent.from_csv_row(_minimal_row())
        assert ev.event_id == 1001
        assert ev.study_id == 999
        assert ev.timestamp == datetime(2026, 1, 15, 8, 0, 0)
        assert ev.location_lat == pytest.approx(52.1)
        assert ev.location_long == pytest.approx(5.0)
        assert ev.individual_id == 42
        assert ev.individual_local_identifier == "Blackbird-A"

    def test_missing_timestamp_raises(self):
        row = _minimal_row()
        del row["timestamp"]
        with pytest.raises(ValueError, match="timestamp"):
            MovebankEvent.from_csv_row(row)

    def test_invalid_timestamp_raises(self):
        with pytest.raises(ValueError, match="timestamp"):
            MovebankEvent.from_csv_row(_minimal_row(timestamp="not-a-date"))

    def test_missing_event_id_generates_deterministic(self):
        row = _minimal_row()
        del row["event_id"]
        ev = MovebankEvent.from_csv_row(row)
        assert isinstance(ev.event_id, int)
        ev2 = MovebankEvent.from_csv_row(row)
        assert ev.event_id == ev2.event_id  # deterministic

    def test_study_id_override(self):
        ev = MovebankEvent.from_csv_row(_minimal_row(study_id=""), study_id=777)
        assert ev.study_id == 777

    def test_out_of_range_lat_nulled(self):
        ev = MovebankEvent.from_csv_row(_minimal_row(location_lat="95.0"))
        assert ev.location_lat is None

    def test_out_of_range_lon_nulled(self):
        ev = MovebankEvent.from_csv_row(_minimal_row(location_long="200.0"))
        assert ev.location_long is None

    def test_optional_fields_default_none(self):
        ev = MovebankEvent.from_csv_row(_minimal_row())
        assert ev.tag_id is None
        assert ev.deployment_id is None
        assert ev.heading is None
        assert ev.deployment_comments is None

    def test_boolean_field_parsed(self):
        ev = MovebankEvent.from_csv_row(_minimal_row(visible="true"))
        assert ev.visible is True

    def test_empty_string_fields_normalized_to_none(self):
        ev = MovebankEvent.from_csv_row(
            _minimal_row(
                individual_sex="",
                tag_model="",
            )
        )
        assert ev.individual_sex is None
        assert ev.tag_model is None


class TestMovebankEventHelpers:
    def _make(self) -> MovebankEvent:
        return MovebankEvent.from_csv_row(_minimal_row())

    def test_to_dict_has_all_fields(self):
        ev = self._make()
        d = ev.to_dict()
        assert set(d.keys()) == {f.name for f in fields(ev)}

    def test_column_names_matches_to_dict(self):
        ev = self._make()
        assert set(MovebankEvent.column_names()) == set(ev.to_dict().keys())

    def test_pandas_dtypes_keys_match_column_names(self):
        assert set(MovebankEvent.pandas_dtypes().keys()) == set(
            MovebankEvent.column_names()
        )

    def test_frozen(self):
        ev = self._make()
        with pytest.raises((AttributeError, TypeError)):
            ev.study_id = 0  # type: ignore[misc]
