"""Tests for avian_biosurveillance.db.models.earth_engine helper functions."""

from __future__ import annotations

import pytest

from avian_biosurveillance.db.models.earth_engine import (
    _as_json_dict,
    _availability_entry,
    _fraction_to_pct,
    _leaf_counts,
    _optional_text,
)


class TestFractionToPct:
    def test_fraction_in_range(self):
        assert _fraction_to_pct(0.5) == pytest.approx(50.0)
        assert _fraction_to_pct(0.0) == pytest.approx(0.0)
        assert _fraction_to_pct(1.0) == pytest.approx(100.0)
        assert _fraction_to_pct(-1.0) == pytest.approx(-100.0)

    def test_value_outside_range_returned_as_is(self):
        # Values outside [-1, 1] are returned rounded, not multiplied
        assert _fraction_to_pct(50.0) == pytest.approx(50.0)
        assert _fraction_to_pct(1.5) == pytest.approx(1.5)

    def test_none_returns_none(self):
        assert _fraction_to_pct(None) is None

    def test_integer_in_range(self):
        assert _fraction_to_pct(0) == pytest.approx(0.0)


class TestOptionalText:
    def test_none_returns_none(self):
        assert _optional_text(None) is None

    def test_empty_string_returns_none(self):
        assert _optional_text("") is None

    def test_sentinel_strings_return_none(self):
        assert _optional_text("None") is None
        assert _optional_text("NULL") is None
        assert _optional_text("null") is None

    def test_normal_string_returned(self):
        assert _optional_text("hello") == "hello"
        assert _optional_text("value") == "value"

    def test_non_string_coerced_to_str(self):
        assert _optional_text(42) == "42"
        assert _optional_text(3.14) == "3.14"


class TestAsJsonDict:
    def test_dict_passthrough(self):
        d = {"a": 1, "b": 2}
        assert _as_json_dict(d) is d

    def test_valid_json_string(self):
        result = _as_json_dict('{"key": "value"}')
        assert result == {"key": "value"}

    def test_invalid_json_returns_empty(self):
        assert _as_json_dict("not json") == {}

    def test_json_list_returns_empty(self):
        assert _as_json_dict("[1, 2, 3]") == {}

    def test_none_returns_empty(self):
        assert _as_json_dict(None) == {}

    def test_int_returns_empty(self):
        assert _as_json_dict(42) == {}


class TestLeafCounts:
    def test_scalar_not_none(self):
        assert _leaf_counts(42) == (1, 1)
        assert _leaf_counts("value") == (1, 1)

    def test_scalar_none(self):
        assert _leaf_counts(None) == (1, 0)

    def test_scalar_sentinel_string(self):
        assert _leaf_counts("none") == (1, 0)
        assert _leaf_counts("null") == (1, 0)
        assert _leaf_counts("nan") == (1, 0)
        assert _leaf_counts("") == (1, 0)

    def test_flat_dict(self):
        total, avail = _leaf_counts({"a": 1, "b": None})
        assert total == 2
        assert avail == 1

    def test_nested_dict(self):
        d = {"outer": {"inner": 42, "missing": None}}
        total, avail = _leaf_counts(d)
        assert total == 2
        assert avail == 1

    def test_error_key_skipped(self):
        d = {"error": "something failed", "data": 42}
        total, avail = _leaf_counts(d)
        # "error" key is skipped
        assert total == 1
        assert avail == 1

    def test_list(self):
        total, avail = _leaf_counts([1, None, 3])
        assert total == 3
        assert avail == 2

    def test_empty_dict(self):
        assert _leaf_counts({}) == (0, 0)

    def test_empty_list(self):
        assert _leaf_counts([]) == (0, 0)


class TestAvailabilityEntry:
    def test_all_available(self):
        entry = _availability_entry(total_fields=5, available_fields=5)
        assert entry["status"] == "available"
        assert entry["completeness_pct"] == 100.0
        assert entry["missing_fields"] == 0

    def test_all_missing(self):
        entry = _availability_entry(total_fields=5, available_fields=0)
        assert entry["status"] == "missing"
        assert entry["completeness_pct"] == 0.0

    def test_partial(self):
        entry = _availability_entry(total_fields=4, available_fields=2)
        assert entry["status"] == "partial"
        assert entry["completeness_pct"] == 50.0

    def test_error(self):
        entry = _availability_entry(
            total_fields=5, available_fields=0, error="API timeout"
        )
        assert entry["status"] == "error"
        assert entry["error"] == "API timeout"

    def test_explicit_status_overrides(self):
        entry = _availability_entry(total_fields=5, available_fields=5, status="custom")
        assert entry["status"] == "custom"

    def test_zero_total_fields(self):
        entry = _availability_entry(total_fields=0, available_fields=0)
        assert entry["completeness_pct"] == 0.0

    def test_keys_present(self):
        entry = _availability_entry(total_fields=3, available_fields=2)
        assert "status" in entry
        assert "available_fields" in entry
        assert "missing_fields" in entry
        assert "total_fields" in entry
        assert "completeness_pct" in entry
