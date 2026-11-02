"""Tests for CDS drought orchestration flows."""

from __future__ import annotations

import importlib
from datetime import date
from unittest.mock import MagicMock, patch

import pytest


cds_drought_flow = importlib.import_module(
    "avian_biosurveillance.orchestration.cds_drought_flow"
)


def _mock_stats(parsed: int = 10, upserted: int = 8) -> MagicMock:
    stats = MagicMock()
    stats.requests_attempted = 1
    stats.requests_downloaded = 1
    stats.requests_failed = 0
    stats.rows_parsed = parsed
    stats.rows_upserted = upserted
    return stats


def test_fetch_cds_drought_profiles_uses_recent_month_collector() -> None:
    collector = MagicMock()
    collector.collect_recent_months.return_value = [_mock_stats()]

    with patch.object(
        cds_drought_flow,
        "CDSDroughtCollector",
        return_value=collector,
    ) as mock_collector:
        cds_drought_flow.fetch_cds_drought_profiles(
            target="database",
            lookback_months=4,
            include_ensemble_members=False,
        )

    mock_collector.assert_called_once_with(include_ensemble_members=False)
    collector.collect_recent_months.assert_called_once_with(4, target="database")


def test_backfill_cds_drought_profiles_passes_force_flag() -> None:
    collector = MagicMock()
    collector.collect_month_range.return_value = [_mock_stats(parsed=4, upserted=4)]

    with patch.object(
        cds_drought_flow,
        "CDSDroughtCollector",
        return_value=collector,
    ) as mock_collector:
        cds_drought_flow.backfill_cds_drought_profiles(
            start_month=date(2024, 1, 1),
            end_month=date(2024, 3, 1),
            force=True,
            target="local",
            include_ensemble_members=True,
        )

    mock_collector.assert_called_once_with(include_ensemble_members=True)
    collector.collect_month_range.assert_called_once_with(
        date(2024, 1, 1),
        date(2024, 3, 1),
        target="local",
        force=True,
    )


def test_backfill_cds_drought_profiles_raises_on_failed_requests() -> None:
    stats = _mock_stats()
    stats.month = "2024-01"
    stats.requests_failed = 1
    collector = MagicMock()
    collector.collect_month_range.return_value = [stats]

    with patch.object(
        cds_drought_flow,
        "CDSDroughtCollector",
        return_value=collector,
    ), pytest.raises(RuntimeError, match="2024-01"):
        cds_drought_flow.backfill_cds_drought_profiles(
            start_month=date(2024, 1, 1),
            end_month=date(2024, 1, 1),
        )


def test_run_cds_drought_flow_delegates_to_task() -> None:
    with patch.object(cds_drought_flow, "fetch_cds_drought_profiles") as mock_task:
        cds_drought_flow.run_cds_drought_flow(
            target="database",
            lookback_months=6,
            include_ensemble_members=False,
        )

    mock_task.assert_called_once_with(
        target="database",
        lookback_months=6,
        include_ensemble_members=False,
    )


def test_run_cds_drought_catchup_flow_rejects_inverted_months() -> None:
    with pytest.raises(ValueError, match="must be >="):
        cds_drought_flow.run_cds_drought_catchup_flow(
            start_month="2025-02",
            end_month="2025-01",
        )
