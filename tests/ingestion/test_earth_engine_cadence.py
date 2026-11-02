"""Tests for the Earth Engine bi-weekly ingestion cadence helpers."""

from __future__ import annotations

from datetime import date

import pytest

from avian_biosurveillance.ingestion.earth_engine_cadence import (
    ANCHOR_DAYS,
    anchor_dates_in_range,
    is_anchor_date,
    latest_anchor_on_or_before,
    next_anchor,
)


def test_anchor_days_are_first_and_fifteenth() -> None:
    assert ANCHOR_DAYS == (1, 15)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2025, 1, 1), True),
        (date(2025, 1, 15), True),
        (date(2025, 1, 2), False),
        (date(2025, 1, 14), False),
        (date(2025, 1, 16), False),
        (date(2025, 1, 31), False),
    ],
)
def test_is_anchor_date(value: date, expected: bool) -> None:
    assert is_anchor_date(value) is expected


def test_anchor_dates_in_range_spans_multiple_months() -> None:
    anchors = anchor_dates_in_range(date(2025, 1, 5), date(2025, 3, 1))
    assert anchors == [
        date(2025, 1, 15),
        date(2025, 2, 1),
        date(2025, 2, 15),
        date(2025, 3, 1),
    ]


def test_anchor_dates_in_range_inclusive_bounds() -> None:
    anchors = anchor_dates_in_range(date(2025, 1, 1), date(2025, 1, 15))
    assert anchors == [date(2025, 1, 1), date(2025, 1, 15)]


def test_anchor_dates_in_range_empty_when_no_anchor() -> None:
    assert anchor_dates_in_range(date(2025, 1, 2), date(2025, 1, 14)) == []


def test_anchor_dates_in_range_empty_when_inverted() -> None:
    assert anchor_dates_in_range(date(2025, 3, 1), date(2025, 1, 1)) == []


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2025, 1, 1), date(2025, 1, 1)),
        (date(2025, 1, 10), date(2025, 1, 1)),
        (date(2025, 1, 15), date(2025, 1, 15)),
        (date(2025, 1, 31), date(2025, 1, 15)),
    ],
)
def test_latest_anchor_on_or_before(value: date, expected: date) -> None:
    assert latest_anchor_on_or_before(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (date(2025, 1, 1), date(2025, 1, 15)),
        (date(2025, 1, 14), date(2025, 1, 15)),
        (date(2025, 1, 15), date(2025, 2, 1)),
        (date(2025, 1, 31), date(2025, 2, 1)),
        (date(2025, 12, 20), date(2026, 1, 1)),
    ],
)
def test_next_anchor(value: date, expected: date) -> None:
    assert next_anchor(value) == expected
