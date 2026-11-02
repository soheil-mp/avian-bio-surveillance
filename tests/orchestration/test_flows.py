"""Tests for BirdWeather backfill gap-detection logic."""

from __future__ import annotations

from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from avian_biosurveillance.orchestration.birdweather_flow import (
    _find_missing_birdweather_dates,
)


# All tests patch ConnectionPool where the helper imports it from.
_POOL_PATCH = "avian_biosurveillance.db.database.ConnectionPool"


def _make_pool(existing_dates: list[date]) -> MagicMock:
    """Build a mock pool whose cursor returns *existing_dates*."""
    cur = MagicMock()
    cur.fetchall.return_value = [(d,) for d in existing_dates]
    pool = MagicMock()
    pool.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    pool.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return pool


# ---------------------------------------------------------------
# _find_missing_birdweather_dates
# ---------------------------------------------------------------


class TestFindMissingBirdweatherDates:
    """Unit tests for the gap-detection helper."""

    @patch(_POOL_PATCH)
    def test_no_gaps(self, mock_get_pool):
        """When every day in the window has data, returns an empty list."""
        start = date(2026, 1, 1)
        end = date(2026, 1, 6)
        existing = [start + timedelta(days=d) for d in range((end - start).days)]
        mock_get_pool.return_value = _make_pool(existing)

        result = _find_missing_birdweather_dates(start, end)
        assert result == []

    @patch(_POOL_PATCH)
    def test_all_missing(self, mock_get_pool):
        """When DB has no data, returns every day in chronological order."""
        mock_get_pool.return_value = _make_pool([])
        start = date(2026, 1, 1)
        end = date(2026, 1, 4)

        result = _find_missing_birdweather_dates(start, end)
        expected = [start + timedelta(days=d) for d in range((end - start).days)]
        assert result == expected

    @patch(_POOL_PATCH)
    def test_partial_gaps(self, mock_get_pool):
        """Only dates without detections are returned, in chronological order."""
        start = date(2026, 1, 1)
        end = date(2026, 1, 6)
        existing = [date(2026, 1, 2), date(2026, 1, 4)]
        mock_get_pool.return_value = _make_pool(existing)

        result = _find_missing_birdweather_dates(start, end)
        expected = [date(2026, 1, 1), date(2026, 1, 3), date(2026, 1, 5)]
        assert result == expected

    @patch(_POOL_PATCH)
    def test_excludes_today(self, mock_get_pool):
        """The exclusive end date is never included in the missing list."""
        mock_get_pool.return_value = _make_pool([])
        start = date(2026, 1, 1)
        end = date(2026, 1, 3)

        result = _find_missing_birdweather_dates(start, end)
        assert end not in result

    @patch(_POOL_PATCH)
    def test_reverse_chronological_order(self, mock_get_pool):
        """Missing dates are returned oldest first."""
        mock_get_pool.return_value = _make_pool([])
        start = date(2026, 1, 1)
        end = date(2026, 1, 5)

        result = _find_missing_birdweather_dates(start, end)
        assert result == sorted(result)
