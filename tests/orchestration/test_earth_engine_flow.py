"""Tests for Earth Engine orchestration flows."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

import avian_biosurveillance.orchestration.earth_engine_flow as earth_engine_flow


class _FrozenDate(date):
    """Date helper with a deterministic today()."""

    @classmethod
    def today(cls) -> _FrozenDate:
        return cls(2025, 1, 10)


def _mock_stats(profiled: int = 10, failed: int = 0) -> MagicMock:
    """Return a collector summary-like mock item."""
    stats = MagicMock()
    stats.locations_profiled = profiled
    stats.locations_failed = failed
    return stats


def test_fetch_earth_engine_grid_profiles_uses_grid_collector() -> None:
    """Bi-weekly grid fetch should profile only anchor dates in the lookback window."""
    collector = MagicMock()
    collector.collect_date_range.return_value = [_mock_stats()]

    with (
        patch.object(earth_engine_flow, "date", _FrozenDate),
        patch.object(
            earth_engine_flow,
            "EarthEngineGridCollector",
            return_value=collector,
        ) as mock_collector,
    ):
        # today=2025-01-10, lookback=10 -> window 2025-01-01..2025-01-10,
        # which contains a single anchor (the 1st).
        earth_engine_flow.fetch_earth_engine_grid_profiles(
            target="database",
            lookback_days=10,
            grid_size_km=7.5,
        )

    mock_collector.assert_called_once_with(
        grid_size_km=7.5,
        max_workers=earth_engine_flow.DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
        cell_workers=earth_engine_flow.DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
        db_batch_size=earth_engine_flow.DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
        high_volume=earth_engine_flow.DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
    )
    collector.collect_date_range.assert_called_once_with(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 1),
        only_dates=[date(2025, 1, 1)],
        target="database",
    )


def test_fetch_earth_engine_grid_profiles_skips_when_no_anchor() -> None:
    """When no bi-weekly anchor falls in the window, no collection should run."""
    collector = MagicMock()

    with (
        patch.object(earth_engine_flow, "date", _FrozenDate),
        patch.object(
            earth_engine_flow,
            "EarthEngineGridCollector",
            return_value=collector,
        ) as mock_collector,
    ):
        # today=2025-01-10, lookback=3 -> window 2025-01-08..2025-01-10 (no anchor).
        earth_engine_flow.fetch_earth_engine_grid_profiles(
            target="database",
            lookback_days=3,
            grid_size_km=7.5,
        )

    mock_collector.assert_not_called()
    collector.collect_date_range.assert_not_called()


def test_backfill_earth_engine_grid_profiles_passes_force_flag() -> None:
    """Grid backfill should forward date range, target, force, and grid size."""
    collector = MagicMock()
    collector.collect_date_range.return_value = [_mock_stats(profiled=4, failed=0)]

    with patch.object(
        earth_engine_flow,
        "EarthEngineGridCollector",
        return_value=collector,
    ) as mock_collector:
        earth_engine_flow.backfill_earth_engine_grid_profiles(
            start_date=date(2024, 12, 1),
            end_date=date(2024, 12, 31),
            force=True,
            target="local",
            grid_size_km=5.0,
        )

    mock_collector.assert_called_once_with(
        grid_size_km=5.0,
        max_workers=earth_engine_flow.DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
        cell_workers=earth_engine_flow.DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
        db_batch_size=earth_engine_flow.DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
        high_volume=earth_engine_flow.DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
    )
    collector.collect_date_range.assert_called_once_with(
        start_date=date(2024, 12, 1),
        end_date=date(2024, 12, 31),
        target="local",
        force=True,
    )


def test_run_earth_engine_grid_flow_delegates_to_task() -> None:
    """Grid flow wrapper should pass arguments through to the grid task."""
    with patch.object(
        earth_engine_flow,
        "fetch_earth_engine_grid_profiles",
    ) as mock_task:
        earth_engine_flow.run_earth_engine_grid_flow(
            target="database",
            lookback_days=5,
            grid_size_km=10.0,
        )

    mock_task.assert_called_once_with(
        target="database",
        lookback_days=5,
        grid_size_km=10.0,
        max_workers=earth_engine_flow.DEFAULT_EARTH_ENGINE_GRID_MAX_WORKERS,
        cell_workers=earth_engine_flow.DEFAULT_EARTH_ENGINE_GRID_CELL_WORKERS,
        db_batch_size=earth_engine_flow.DEFAULT_EARTH_ENGINE_GRID_DB_BATCH_SIZE,
        high_volume=earth_engine_flow.DEFAULT_EARTH_ENGINE_HIGH_VOLUME,
    )


def test_run_earth_engine_grid_repair_flow_rejects_inverted_dates() -> None:
    """Repair flow should reject an end date earlier than start date."""
    with pytest.raises(ValueError, match="must be >="):
        earth_engine_flow.run_earth_engine_grid_repair_flow(
            start_date="2025-01-10",
            end_date="2025-01-09",
        )
